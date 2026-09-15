from __future__ import annotations

import asyncio
import math
from pathlib import Path

from ragtone.checkpoints import CheckpointStore
from ragtone.chunking import chat_chunk, confluence_chunks, jira_chunks, split_markdown_sections
from ragtone.embeddings import HashEmbedder
from ragtone.index import hybrid_search_body
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


def test_confluence_splits_on_headings() -> None:
    body = "# Intro\nwelcome\n\n## Setup\ndo this\n\n## Run\ndo that\n"
    sections = split_markdown_sections(body, "Page")
    assert [title for title, _ in sections] == ["Intro", "Setup", "Run"]
    chunks = confluence_chunks(page_id="42", title="Page", body=body)
    assert [chunk.native_id for chunk in chunks] == ["42:0", "42:1", "42:2"]
    assert chunks[1].parent_id == "42"
    assert "do this" in chunks[1].text


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


def test_hybrid_body_uses_rrf_and_filters() -> None:
    body = hybrid_search_body(
        query="login timeout",
        vector=[0.1, 0.2],
        filters=Filters(source="jira", channel="eng", since="2026-01-01"),
        k=8,
    )
    rrf = body["retriever"]["rrf"]
    kinds = {next(iter(item)) for item in rrf["retrievers"]}
    assert kinds == {"standard", "knn"}
    knn = next(item["knn"] for item in rrf["retrievers"] if "knn" in item)
    assert knn["field"] == "embedding"
    assert knn["filter"]["bool"]["filter"][0] == {"term": {"source": "jira"}}
    lexical = next(item["standard"]["query"] for item in rrf["retrievers"] if "standard" in item)
    assert lexical["bool"]["filter"][2] == {"range": {"updated_at": {"gte": "2026-01-01"}}}


class _ScriptedConnector:
    name = "jira"

    def __init__(self, chunks: list[Chunk], watermark: str) -> None:
        self._chunks = chunks
        self._watermark = watermark
        self._sent = False

    async def next_page(
        self, checkpoint: str | None, *, backfill: bool, cursor, backfill_days=None
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
        self, checkpoint: str | None, *, backfill: bool, cursor, backfill_days=None
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
    store = InMemoryIndex()
    chunks = jira_chunks(key="ABC-7", summary="VPN down", description="No tunnel")
    store.upsert(chunks, embedder.embed([c.text for c in chunks]))
    mcp = build_mcp(RetrievalService(store, embedder))
    tools = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert tools >= {"search", "thread", "issue", "page"}
    search = asyncio.run(mcp.call_tool("search", {"query": "VPN tunnel"}))
    issue = asyncio.run(mcp.call_tool("issue", {"key": "ABC-7"}))
    assert "ABC-7" in _tool_text(search)
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
