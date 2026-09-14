from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.testclient import TestClient

from ragtone.admin import idle_job, snapshot
from ragtone.board_http import create_app
from ragtone.checkpoints import CheckpointStore
from ragtone.chunking import chat_chunk, jira_chunks
from ragtone.embeddings import HashEmbedder
from ragtone.ingest.base import FetchResult
from ragtone.ingest.worker import IngestWorker
from ragtone.memory_index import InMemoryIndex
from ragtone.settings import FoundationMcp, Settings


class _NamedConnector:
    def __init__(self, name: str, chunks, watermark: str) -> None:
        self.name = name
        self._chunks = chunks
        self._watermark = watermark
        self.calls = 0

    async def fetch(self, checkpoint: str | None, *, backfill: bool) -> FetchResult:
        self.calls += 1
        return FetchResult(chunks=self._chunks, watermark=self._watermark)


def test_memory_index_stats_count_by_source() -> None:
    store = InMemoryIndex()
    docs = jira_chunks(key="ABC-1", summary="Login", description="Timeout") + [
        chat_chunk(message_id="1.0", text="caiu o sso", channel="eng", thread_id="1.0")
    ]
    store.upsert(docs, HashEmbedder(8).embed([chunk.text for chunk in docs]))
    stats = store.stats()
    assert stats["ok"] is True
    assert stats["total"] == 2
    assert stats["by_source"] == {"jira": 1, "chat": 1}


def test_snapshot_flags_disabled_connectors_when_es_is_down() -> None:
    settings = Settings()
    data = snapshot(
        settings,
        stats={"ok": False, "total": 0, "by_source": {}},
        checkpoints={},
        recent=[],
        job=idle_job(),
    )
    codes = {(gap["code"], gap["connector"]) for gap in data["gaps"]}
    assert ("es_down", None) in codes
    assert ("disabled", "jira") in codes
    assert ("disabled", "confluence") in codes
    assert ("disabled", "chat") in codes
    assert all(not row["can_sync"] for row in data["connectors"])


def test_snapshot_flags_enabled_jira_that_never_ran() -> None:
    settings = Settings(
        jira={"enabled": True, "mcp": "atlassian", "projects": ["ABC"]},
        foundation_mcps=[
            FoundationMcp(name="atlassian", url="http://127.0.0.1:3001/mcp"),
        ],
    )
    data = snapshot(
        settings,
        stats={"ok": True, "total": 0, "by_source": {}},
        checkpoints={},
        recent=[],
        job=idle_job(),
    )
    jira = next(row for row in data["connectors"] if row["name"] == "jira")
    assert jira["can_sync"] is True
    jira_gaps = {gap["code"] for gap in data["gaps"] if gap["connector"] == "jira"}
    assert jira_gaps == {"never_ran", "zero_chunks"}
    assert "disabled" not in jira_gaps


def test_snapshot_flags_chat_without_channels() -> None:
    settings = Settings(
        chat={"enabled": True, "mcp": "chat", "channels": []},
        foundation_mcps=[FoundationMcp(name="chat", url="http://127.0.0.1:3002/mcp")],
    )
    data = snapshot(
        settings,
        stats={"ok": True, "total": 0, "by_source": {}},
        checkpoints={},
        recent=[],
        job=idle_job(),
    )
    codes = {gap["code"] for gap in data["gaps"] if gap["connector"] == "chat"}
    assert "chat_no_channels" in codes
    assert "mcp_missing" not in codes


def test_snapshot_flags_jira_without_projects() -> None:
    settings = Settings(
        jira={"enabled": True, "mcp": "atlassian"},
        foundation_mcps=[FoundationMcp(name="atlassian", url="http://127.0.0.1:3001/mcp")],
    )
    data = snapshot(
        settings,
        stats={"ok": True, "total": 0, "by_source": {}},
        checkpoints={},
        recent=[],
        job=idle_job(),
    )
    jira = next(row for row in data["connectors"] if row["name"] == "jira")
    assert jira["can_sync"] is False
    assert jira["watching"] == []
    codes = {gap["code"] for gap in data["gaps"] if gap["connector"] == "jira"}
    assert "jira_no_boards" in codes


def test_placeholder_mcp_url_is_not_configured() -> None:
    settings = Settings(
        jira={"enabled": True, "mcp": "atlassian"},
        foundation_mcps=[
            FoundationMcp(
                name="atlassian",
                url="http://127.0.0.1:9/replace-with-foundation-atlassian-mcp",
            )
        ],
    )
    data = snapshot(
        settings,
        stats={"ok": False, "total": 0, "by_source": {}},
        checkpoints={},
        recent=[],
        job=idle_job(),
    )
    jira = next(row for row in data["connectors"] if row["name"] == "jira")
    assert jira["mcp_configured"] is False
    assert jira["can_sync"] is False


def test_poll_named_runs_only_that_connector(tmp_path: Path) -> None:
    jira = _NamedConnector(
        "jira",
        jira_chunks(key="ABC-1", summary="Login", description="Timeout"),
        "2026-03-01",
    )
    chat = _NamedConnector(
        "chat",
        [chat_chunk(message_id="1.0", text="sso", channel="eng", thread_id="1.0")],
        "1.0",
    )
    store = InMemoryIndex()
    checkpoints = CheckpointStore(tmp_path / "checkpoints.json")
    worker = IngestWorker(
        store,
        HashEmbedder(8),
        checkpoints,
        [jira, chat],
        poll_seconds=1,
    )
    assert asyncio.run(worker.poll_named("chat")) == 1
    assert chat.calls == 1
    assert jira.calls == 0
    assert checkpoints.get("chat") == "1.0"
    assert checkpoints.get("jira") is None
    assert store.stats()["by_source"] == {"chat": 1}


def test_board_http_admin_when_index_is_down(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    data = client.get("/api/admin").json()
    assert data["index"]["ok"] is False
    assert data["recent"] == []
    codes = {gap["code"] for gap in data["gaps"]}
    assert "es_down" in codes
    assert "disabled" in codes
    assert data["job"]["status"] == "idle"


def test_board_http_sync_rejects_disabled_connector(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    response = client.post("/api/admin/sync", json={"name": "jira"})
    assert response.status_code == 400
    assert "desligado" in response.json()["error"]


def test_board_http_sync_rejects_placeholder_mcp(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        embedder="hash",
        jira={"enabled": True, "mcp": "atlassian"},
        foundation_mcps=[
            FoundationMcp(
                name="atlassian",
                url="http://127.0.0.1:9/replace-with-foundation-atlassian-mcp",
            )
        ],
    )
    client = TestClient(create_app(settings))
    response = client.post("/api/admin/sync", json={"name": "jira"})
    assert response.status_code == 400
    assert "MCP" in response.json()["error"]


def test_board_http_sync_conflict_when_job_running(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        embedder="hash",
        jira={"enabled": True, "mcp": "atlassian", "projects": ["ABC"]},
        foundation_mcps=[FoundationMcp(name="atlassian", url="http://127.0.0.1:3001/mcp")],
    )
    client = TestClient(create_app(settings))
    client.app.state.ctx.job = {
        "status": "running",
        "connector": "jira",
        "backfill": False,
        "chunks": 0,
        "error": None,
    }
    response = client.post("/api/admin/sync", json={"name": "jira"})
    assert response.status_code == 409
    assert "curso" in response.json()["error"]
