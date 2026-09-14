from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.testclient import TestClient

from ragtone.board_http import create_app
from ragtone.ingest.confluence import ConfluenceConnector, scoped_cql
from ragtone.ingest.jira import JiraConnector, scoped_jql
from ragtone.settings import ChatSource, ConfluenceSource, FoundationMcp, JiraSource, Settings
from ragtone.watches import WatchStore, clean_watch_item, overlay_settings, parse_items


class _BoomCaller:
    async def call_tool(self, *args, **kwargs):
        raise AssertionError("MCP should not be called")


def test_clean_watch_item_rejects_jql_injection() -> None:
    assert clean_watch_item("jira", "ABC) OR 1=1") is None
    assert clean_watch_item("jira", "abc") == "ABC"
    assert clean_watch_item("chat", "#eng") == "eng"
    assert clean_watch_item("confluence", "123456") == "123456"
    assert clean_watch_item("confluence", "docs") == "DOCS"


def test_watch_store_overrides_yaml(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, chat={"channels": ["yaml"]})
    store = WatchStore(tmp_path / "watches.json")
    assert [item["id"] for item in store.resolved(settings)["chat"]] == ["yaml"]
    store.set("chat", ["ops", "eng"])
    again = WatchStore(tmp_path / "watches.json")
    assert [item["id"] for item in again.resolved(settings)["chat"]] == ["ops", "eng"]
    assert again.resolved(settings)["jira"] == []


def test_overlay_settings_copies_watches() -> None:
    settings = Settings(chat={"channels": ["old"]})
    next_ = overlay_settings(settings, {"chat": ["new"], "jira": ["ABC"], "confluence": ["ENG"]})
    assert next_.chat.channels == ["new"]
    assert next_.jira.projects == ["ABC"]
    assert next_.confluence.docs == ["ENG"]
    assert settings.chat.channels == ["old"]


def test_overlay_settings_keeps_channel_windows() -> None:
    settings = Settings()
    next_ = overlay_settings(
        settings,
        {"chat": [{"id": "eng", "backfill_days": 90}], "jira": [], "confluence": []},
    )
    assert next_.chat.channels == ["eng"]
    assert next_.chat.channel_windows == {"eng": 90}


def test_scoped_jql_puts_projects_before_order_by() -> None:
    jql = scoped_jql(
        ["ABC", "PLAT"],
        'updated >= "{checkpoint}" ORDER BY updated ASC',
        "2026-01-01",
    )
    assert jql.startswith("project in (ABC, PLAT) AND")
    assert jql.endswith("ORDER BY updated ASC")
    assert "ORDER BY" not in jql[jql.index("(") : jql.index("ORDER BY")]


def test_scoped_cql_covers_spaces_and_pages() -> None:
    cql = scoped_cql(["ENG", "42"], 'lastModified >= "{checkpoint}"', "2026-01-01")
    assert "space in (ENG)" in cql
    assert "id in (42)" in cql
    assert "ancestor in (42)" in cql


def test_jira_skips_mcp_when_no_projects() -> None:
    connector = JiraConnector(
        JiraSource(enabled=True, projects=[]),
        _BoomCaller(),
        backfill_days=30,
        pause=0,
    )
    result = asyncio.run(connector.fetch(None, backfill=True))
    assert result.chunks == []


def test_confluence_skips_mcp_when_no_docs() -> None:
    connector = ConfluenceConnector(
        ConfluenceSource(enabled=True, docs=[]),
        _BoomCaller(),
        backfill_days=30,
        pause=0,
    )
    result = asyncio.run(connector.fetch(None, backfill=True))
    assert result.chunks == []


def test_board_http_saves_watches(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    saved = client.post("/api/admin/watches", json={"name": "jira", "items": ["abc", "PLAT"]})
    assert saved.status_code == 200
    jira = next(row for row in saved.json()["connectors"] if row["name"] == "jira")
    assert jira["watching"] == ["ABC", "PLAT"]
    again = client.get("/api/admin").json()
    assert again["watches"]["jira"] == ["ABC", "PLAT"]
    chat = client.post("/api/admin/watches", json={"name": "chat", "items": ["#eng"]})
    assert chat.json()["watches"]["chat"] == ["eng"]


def test_board_http_rejects_invalid_watch_item(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/admin/watches",
        json={"name": "jira", "items": ["ABC) OR project = HACK"]},
    )
    assert response.status_code == 400


def test_board_http_sync_rejects_jira_without_projects(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        embedder="hash",
        jira={"enabled": True, "mcp": "atlassian"},
        foundation_mcps=[FoundationMcp(name="atlassian", url="http://127.0.0.1:3001/mcp")],
    )
    client = TestClient(create_app(settings))
    response = client.post("/api/admin/sync", json={"name": "jira"})
    assert response.status_code == 400
    assert "projetos" in response.json()["error"]


def test_parse_items_dedupes() -> None:
    assert parse_items("chat", ["eng", "#eng", "ENG"]) == ["eng"]


def test_board_http_saves_channel_backfill_range(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    saved = client.post(
        "/api/admin/watches",
        json={"name": "chat", "items": [{"id": "eng", "backfill_days": 90}]},
    )
    assert saved.status_code == 200
    chat = next(row for row in saved.json()["connectors"] if row["name"] == "chat")
    assert chat["watching"] == ["eng"]
    assert chat["targets"][0]["backfill_days"] == 90
    stored = (tmp_path / "watches.json").read_text()
    assert "90" in stored


def test_board_http_rejects_invalid_backfill_days(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/admin/watches",
        json={"name": "chat", "items": [{"id": "eng", "backfill_days": 99999}]},
    )
    assert response.status_code == 400


def test_chat_uses_channel_window_until_checkpoint(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from ragtone.checkpoints import CheckpointStore
    from ragtone.ingest.chat import ChatConnector

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call_tool(self, name: str, args: dict) -> dict:
            self.calls.append((name, args))
            return {"messages": []}

    caller = Recorder()
    checkpoints = CheckpointStore(tmp_path / "checkpoints.json")
    connector = ChatConnector(
        ChatSource(enabled=True, channels=["eng"], channel_windows={"eng": 30}),
        caller,
        pause=0,
        default_days=365,
        checkpoints=checkpoints,
    )
    asyncio.run(connector.fetch(None, backfill=False))
    oldest = caller.calls[0][1]["oldest"]
    age = datetime.now(timezone.utc).timestamp() - float(oldest)
    assert 29 * 86400 < age < 31 * 86400
    checkpoints.set("chat:eng", "1700000000.000000")
    asyncio.run(connector.fetch(None, backfill=False))
    assert caller.calls[-1][1]["oldest"] == "1700000000.000000"
    asyncio.run(connector.fetch(None, backfill=True))
    assert caller.calls[-1][1]["oldest"] != "1700000000.000000"


def test_chat_everything_window_uses_oldest_zero() -> None:
    from ragtone.ingest.chat import ChatConnector

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call_tool(self, name: str, args: dict) -> dict:
            self.calls.append((name, args))
            return {"messages": []}

    caller = Recorder()
    connector = ChatConnector(
        ChatSource(enabled=True, channels=["eng"], channel_windows={"eng": 0}),
        caller,
        pause=0,
        default_days=365,
    )
    asyncio.run(connector.fetch(None, backfill=False))
    assert caller.calls[0][1]["oldest"] == "0"
