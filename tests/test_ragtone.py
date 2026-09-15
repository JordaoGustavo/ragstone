from __future__ import annotations

import asyncio
import math
from pathlib import Path

from ragtone.checkpoints import CheckpointStore
from ragtone.chunking import chat_chunk, confluence_chunks, jira_chunks, split_markdown_sections
from ragtone.embeddings import HashEmbedder
from ragtone.index import hybrid_search_body, identifier_values, lexical_search_body
from ragtone.ingest.base import FetchResult, Page, WorkRecord
from ragtone.ingest.parse import as_records, as_text
from ragtone.ingest.worker import IngestWorker
from ragtone.mcp_server import build_mcp, run_mcp
from ragtone.memory_index import InMemoryIndex
from ragtone.models import Chunk, Filters
from ragtone.retrieval import RetrievalService
from ragtone.settings import load_settings


def test_chunk_ids_are_source_plus_native_id() -> None:
    chunk = Chunk(source="jira", native_id="ABC-1", text="hello")
    assert chunk.id == "jira:ABC-1"


def test_chat_chunk_titles_the_message_not_the_channel() -> None:
    chunk = chat_chunk(
        message_id="1710000000.000100",
        text="SSO caiu no gateway de auth",
        channel="C024BE7LT",
    )
    assert chunk.title == "SSO caiu no gateway de auth"
    assert chunk.channel_or_space == "C024BE7LT"
    assert chunk.native_id == "1710000000.000100"


def test_confluence_splits_on_headings() -> None:
    body = "# Intro\nwelcome\n\n## Setup\ndo this\n\n## Run\ndo that\n"
    sections = split_markdown_sections(body, "Page")
    assert [title for title, _ in sections] == ["Intro", "Setup", "Run"]
    chunks = confluence_chunks(page_id="42", title="Page", body=body)
    assert [chunk.native_id for chunk in chunks] == ["42", "42:0", "42:1", "42:2"]
    assert chunks[0].title == "Page"
    assert chunks[2].parent_id == "42"
    assert "do this" in chunks[2].text


def test_jira_issue_and_comments_are_separate_chunks() -> None:
    chunks = jira_chunks(
        key="ABC-9",
        summary="Fix login",
        description="Users cannot sign in",
        comments=[{"id": "c1", "body": "still broken", "author": "ana"}],
    )
    assert chunks[0].id == "jira:ABC-9"
    assert chunks[1].id == "jira:ABC-9:comment:c1"
    assert chunks[1].parent_id == "ABC-9"


def test_checkpoints_round_trip(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints.json")
    assert store.get("jira") is None
    store.set("jira", "2026-01-15")
    again = CheckpointStore(tmp_path / "checkpoints.json")
    assert again.get("jira") == "2026-01-15"


def test_hash_embedder_is_normalized_and_stable() -> None:
    embedder = HashEmbedder(8)
    first = embedder.embed(["login timeout"])[0]
    second = embedder.embed(["login timeout"])[0]
    other = embedder.embed(["unrelated"])[0]
    assert first == second
    assert first != other
    assert math.isclose(math.sqrt(sum(v * v for v in first)), 1.0, rel_tol=1e-6)


def test_hybrid_body_uses_knn_query_and_filters() -> None:
    body = hybrid_search_body(
        query="login timeout ABC-9",
        vector=[0.1, 0.2],
        filters=Filters(source="jira", channel="eng", since="2026-01-01"),
        k=8,
    )
    assert "retriever" not in body
    assert "rank" not in body
    query = body["query"]["bool"]
    assert query["filter"][0] == {"term": {"source": "jira"}}
    assert query["filter"][2] == {"range": {"updated_at": {"gte": "2026-01-01"}}}
    knn = next(item["knn"] for item in query["should"] if "knn" in item)
    assert knn["field"] == "embedding"
    assert knn["k"] == 8
    assert knn["boost"] == 2.0
    match = next(item["multi_match"] for item in query["should"] if "multi_match" in item)
    assert "title^2" in match["fields"]
    assert any(
        item.get("term", {}).get("native_id", {}).get("value") == "ABC-9"
        for item in query["should"]
    )


def test_lexical_body_skips_knn() -> None:
    body = lexical_search_body(
        query="login timeout ABC-9",
        filters=Filters(source="jira", channel="eng", since="2026-01-01"),
        k=12,
    )
    assert "retriever" not in body
    query = body["query"]["bool"]
    assert query["filter"][0] == {"term": {"source": "jira"}}
    assert all("knn" not in item for item in query["should"])
    match = next(item["multi_match"] for item in query["should"] if "multi_match" in item)
    assert "title^2" in match["fields"]
    assert any(
        item.get("term", {}).get("native_id", {}).get("value") == "ABC-9"
        for item in query["should"]
    )


def test_identifier_values_extract_issue_keys() -> None:
    assert identifier_values("ABC-12") == ["ABC-12"]
    assert identifier_values("timeout no ABC-12 e NET-1") == [
        "timeout no ABC-12 e NET-1",
        "ABC-12",
        "NET-1",
    ]


class _ScriptedConnector:
    name = "jira"

    def __init__(self, chunks: list[Chunk], watermark: str) -> None:
        self._chunks = chunks
        self._watermark = watermark
        self._sent = False

    async def next_page(
        self, checkpoint: str | None, *, backfill: bool, cursor, backfill_days=None, targets=None
    ) -> Page:
        if self._sent:
            return Page(done=True)
        self._sent = True
        ref = self._chunks[0].parent_id or self._chunks[0].native_id
        return Page(
            records=[WorkRecord(ref=ref, payload={}, watermark=self._watermark)],
            total=1,
            done=True,
        )

    async def materialize(self, record: WorkRecord) -> FetchResult:
        return FetchResult(chunks=self._chunks, watermark=self._watermark)


def test_ingest_worker_upserts_and_saves_watermark(tmp_path: Path) -> None:
    chunks = jira_chunks(key="ABC-1", summary="Login", description="Timeout on SSO")
    store = InMemoryIndex()
    checkpoints = CheckpointStore(tmp_path / "checkpoints.json")
    worker = IngestWorker(
        store,
        HashEmbedder(8),
        checkpoints,
        [_ScriptedConnector(chunks, "2026-03-01")],
        poll_seconds=1,
    )
    asyncio.run(worker.backfill())
    hits = store.by_issue("ABC-1")
    assert [hit.native_id for hit in hits] == ["ABC-1"]
    assert checkpoints.get("jira") == "2026-03-01"


class _BoomConnector:
    name = "confluence"

    async def next_page(
        self, checkpoint: str | None, *, backfill: bool, cursor, backfill_days=None, targets=None
    ) -> Page:
        raise RuntimeError("upstream 500")

    async def materialize(self, record: WorkRecord) -> FetchResult:
        raise RuntimeError("upstream 500")


def test_ingest_worker_isolates_a_failing_connector(tmp_path: Path) -> None:
    chunks = jira_chunks(key="ABC-1", summary="Login", description="Timeout on SSO")
    store = InMemoryIndex()
    checkpoints = CheckpointStore(tmp_path / "checkpoints.json")
    worker = IngestWorker(
        store,
        HashEmbedder(8),
        checkpoints,
        [_BoomConnector(), _ScriptedConnector(chunks, "2026-03-01")],
        poll_seconds=1,
    )
    count = asyncio.run(worker.backfill())
    assert count == 1
    assert [hit.native_id for hit in store.by_issue("ABC-1")] == ["ABC-1"]


def test_recent_messages_are_newest_thread_first() -> None:
    embedder = HashEmbedder(8)
    store = InMemoryIndex()
    docs = [
        chat_chunk(
            message_id="1.0",
            text="old in thread",
            channel="eng",
            thread_id="1.0",
            created_at="2026-01-01T00:00:00",
        ),
        chat_chunk(
            message_id="2.0",
            text="other channel",
            channel="ops",
            thread_id="2.0",
            created_at="2026-02-01T00:00:00",
        ),
        chat_chunk(
            message_id="1.1",
            text="new in thread",
            channel="eng",
            thread_id="1.0",
            created_at="2026-03-01T00:00:00",
        ),
    ]
    store.upsert(docs, embedder.embed([chunk.text for chunk in docs]))
    hits = RetrievalService(store, embedder).recent(k=10)
    assert [hit["text"] for hit in hits] == ["new in thread", "other channel"]


def test_recent_without_fallback_stays_on_the_source() -> None:
    embedder = HashEmbedder(8)
    store = InMemoryIndex()
    docs = [
        chat_chunk(
            message_id="1.0",
            text="only chat",
            channel="eng",
            thread_id="1.0",
            created_at="2026-03-01T00:00:00",
        ),
        *jira_chunks(key="ABC-1", summary="VPN", description="tunnel down"),
    ]
    store.upsert(docs, embedder.embed([chunk.text for chunk in docs]))
    service = RetrievalService(store, embedder)
    jira = service.recent(source="jira", fallback=False)
    assert {hit["source"] for hit in jira} == {"jira"}
    missing = service.recent(source="confluence", fallback=False)
    assert missing == []
    mixed = service.recent(source="confluence", fallback=True)
    assert mixed


def test_recent_named_source_stays_in_that_connector() -> None:
    embedder = HashEmbedder(8)
    store = InMemoryIndex()
    docs = [
        chat_chunk(
            message_id="1.0",
            text="sso caiu",
            channel="eng",
            thread_id="1.0",
        )
    ]
    store.upsert(docs, embedder.embed([chunk.text for chunk in docs]))
    service = RetrievalService(store, embedder)
    assert service.recent(source="jira", k=10, fallback=False) == []
    mixed = service.recent(source="jira", k=10)
    assert [hit["source"] for hit in mixed] == ["chat"]


def test_retrieval_search_prefers_the_matching_issue() -> None:
    embedder = HashEmbedder(32)
    store = InMemoryIndex()
    relevant = jira_chunks(key="ABC-1", summary="SSO login timeout", description="Users stuck")
    noise = jira_chunks(key="ABC-2", summary="Printer jam", description="Third floor")
    store.upsert(relevant + noise, embedder.embed([c.text for c in relevant + noise]))
    service = RetrievalService(store, embedder)
    hits = service.search("SSO login timeout", source="jira")
    assert hits[0]["native_id"] == "ABC-1"


def test_retrieval_search_finds_issue_by_key() -> None:
    embedder = HashEmbedder(32)
    store = InMemoryIndex()
    relevant = jira_chunks(key="ABC-12", summary="SSO login timeout", description="Users stuck")
    noise = jira_chunks(key="NET-1", summary="Printer jam", description="Third floor")
    store.upsert(relevant + noise, embedder.embed([c.text for c in relevant + noise]))
    service = RetrievalService(store, embedder)
    hits = service.search("ABC-12")
    assert hits[0]["native_id"] == "ABC-12"


def _mixed_search_docs():
    parent = chat_chunk(
        message_id="1.0",
        text="what broke?",
        channel="eng",
        thread_id="1.0",
        created_at="1.0",
    )
    reply = chat_chunk(
        message_id="1.1",
        text="the SSO gateway exploded",
        channel="eng",
        thread_id="1.0",
        created_at="1.1",
    )
    pages = confluence_chunks(
        page_id="99",
        title="Runbook",
        body="## Symptoms\ntimeout\n\n## Fix\nrestart sso\n",
    )
    issue = jira_chunks(
        key="ABC-9",
        summary="Login",
        description="Users cannot sign in",
        comments=[{"id": "c1", "body": "VPN tunnel is down", "author": "ana"}],
    )
    return [parent, reply, *pages, *issue]


class _RecordingIndex(InMemoryIndex):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def search(self, query, vector, filters, k=8):
        self.calls.append("semantic")
        return super().search(query, vector, filters, k)

    def lexical_search(self, query, filters, k=8):
        self.calls.append("lexical")
        return super().lexical_search(query, filters, k)


class _BoomEmbedder:
    dims = 32

    def embed(self, texts):
        raise AssertionError("spotlight must not embed")


def test_spotlight_search_returns_roots_not_chunks() -> None:
    embedder = HashEmbedder(32)
    store = InMemoryIndex()
    docs = _mixed_search_docs()
    store.upsert(docs, embedder.embed([chunk.text for chunk in docs]))
    service = RetrievalService(store, _BoomEmbedder())
    thread = service.spotlight("SSO gateway exploded", source="chat")
    assert [hit["native_id"] for hit in thread] == ["1.0"]
    page = service.spotlight("restart sso", source="confluence")
    assert [hit["native_id"] for hit in page] == ["99"]
    assert page[0]["title"] == "Runbook"
    ticket = service.spotlight("VPN tunnel", source="jira")
    assert [hit["native_id"] for hit in ticket] == ["ABC-9"]


def test_mcp_search_returns_matching_chunks() -> None:
    embedder = HashEmbedder(32)
    store = InMemoryIndex()
    docs = _mixed_search_docs()
    store.upsert(docs, embedder.embed([chunk.text for chunk in docs]))
    service = RetrievalService(store, embedder)
    thread = service.search("SSO gateway exploded", source="chat")
    assert thread[0]["native_id"] == "1.1"
    ticket = service.search("VPN tunnel", source="jira")
    assert ticket[0]["native_id"] == "ABC-9:comment:c1"


def test_spotlight_and_mcp_use_different_queries() -> None:
    embedder = HashEmbedder(32)
    store = _RecordingIndex()
    docs = _mixed_search_docs()
    store.upsert(docs, embedder.embed([chunk.text for chunk in docs]))
    service = RetrievalService(store, embedder)
    service.spotlight("SSO gateway exploded", source="chat")
    service.search("SSO gateway exploded", source="chat")
    assert store.calls == ["lexical", "semantic"]


def test_retrieval_expands_thread_and_page() -> None:
    embedder = HashEmbedder(8)
    store = InMemoryIndex()
    parent = chat_chunk(
        message_id="1.0",
        text="what broke?",
        channel="eng",
        thread_id="1.0",
        created_at="1.0",
    )
    reply = chat_chunk(
        message_id="1.1",
        text="the SSO gateway",
        channel="eng",
        thread_id="1.0",
        created_at="1.1",
    )
    pages = confluence_chunks(
        page_id="99",
        title="Runbook",
        body="## Symptoms\ntimeout\n\n## Fix\nrestart sso\n",
    )
    docs = [parent, reply, *pages]
    store.upsert(docs, embedder.embed([c.text for c in docs]))
    service = RetrievalService(store, embedder)
    thread = service.thread("1.0")
    assert [item["native_id"] for item in thread] == ["1.0", "1.1"]
    page = service.page("99")
    assert {item["title"] for item in page} == {"Symptoms", "Fix"}


def _tool_text(result) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def test_mcp_tools_search_and_issue_return_json() -> None:
    embedder = HashEmbedder(16)
    store = _RecordingIndex()
    chunks = jira_chunks(
        key="ABC-7",
        summary="VPN down",
        description="No tunnel",
        comments=[{"id": "c1", "body": "VPN tunnel is down", "author": "ana"}],
    )
    store.upsert(chunks, embedder.embed([c.text for c in chunks]))
    mcp = build_mcp(RetrievalService(store, embedder))
    tools = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert tools >= {"search", "thread", "issue", "page"}
    search = asyncio.run(mcp.call_tool("search", {"query": "VPN tunnel"}))
    issue = asyncio.run(mcp.call_tool("issue", {"key": "ABC-7"}))
    text = _tool_text(search)
    assert "ABC-7:comment:c1" in text
    assert store.calls == ["semantic"]
    assert "VPN down" in _tool_text(issue)


def test_mcp2_refuses_lan_bind() -> None:
    embedder = HashEmbedder(4)
    mcp = build_mcp(RetrievalService(InMemoryIndex(), embedder))
    try:
        run_mcp(mcp, "0.0.0.0", 8765)
    except ValueError as exc:
        assert "localhost" in str(exc)
    else:
        raise AssertionError("expected localhost-only bind")


def test_as_text_flattens_adf_and_as_records_unwraps_issues() -> None:
    adf = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
    }
    assert as_text(adf) == "hello"
    records = as_records({"issues": [{"key": "A-1"}, {"key": "A-2"}]}, "issues")
    assert [item["key"] for item in records] == ["A-1", "A-2"]


def test_load_settings_reads_yaml(tmp_path: Path) -> None:
    path = tmp_path / "ragtone.yaml"
    path.write_text("mcp_port: 9001\njira:\n  enabled: true\n  mcp: atlassian\n")
    settings = load_settings(path)
    assert settings.mcp_port == 9001
    assert settings.jira.enabled is True
    assert settings.jira.mcp == "atlassian"


def test_load_settings_merges_local_yaml(tmp_path: Path) -> None:
    (tmp_path / "ragtone.yaml").write_text(
        "mcp_port: 9001\n"
        "jira:\n"
        "  enabled: false\n"
        "  mcp: atlassian\n"
        "  projects: []\n"
        "foundation_mcps:\n"
        "  - name: atlassian\n"
        "    transport: http\n"
        "    url: http://placeholder\n"
    )
    (tmp_path / "ragtone.local.yaml").write_text(
        "mcp_port: 9002\n"
        "jira:\n"
        "  enabled: true\n"
        "  projects: [ABC]\n"
        "foundation_mcps:\n"
        "  - name: atlassian\n"
        "    transport: http\n"
        "    url: http://127.0.0.1:9/real-gateway\n"
    )
    settings = load_settings(tmp_path / "ragtone.yaml")
    assert settings.mcp_port == 9002
    assert settings.jira.enabled is True
    assert settings.jira.mcp == "atlassian"
    assert settings.jira.projects == ["ABC"]
    assert settings.foundation_mcps[0].url == "http://127.0.0.1:9/real-gateway"


def test_load_settings_skips_local_when_config_is_already_local(tmp_path: Path) -> None:
    (tmp_path / "ragtone.yaml").write_text("mcp_port: 9001\n")
    local = tmp_path / "ragtone.local.yaml"
    local.write_text("mcp_port: 9002\n")
    (tmp_path / "ragtone.local.local.yaml").write_text("mcp_port: 1\n")
    settings = load_settings(local)
    assert settings.mcp_port == 9002
