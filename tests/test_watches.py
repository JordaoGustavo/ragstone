from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.testclient import TestClient

from ragtone.board_http import create_app
from ragtone.ingest.confluence import ConfluenceConnector, scoped_cql
from ragtone.ingest.jira import JiraConnector, scoped_jql
from ragtone.settings import ChatSource, ConfluenceSource, FoundationMcp, JiraSource, Settings
from ragtone.watches import WatchStore, clean_watch_item, overlay_settings, parse_cutoff, parse_items


class _BoomCaller:
    async def call_tool(self, *args, **kwargs):
        raise AssertionError("MCP should not be called")


def test_clean_watch_item_rejects_jql_injection() -> None:
    assert clean_watch_item("jira", "ABC) OR 1=1") is None
    assert clean_watch_item("jira", "abc") == "ABC"
    assert clean_watch_item("chat", "#eng") == "eng"
    assert clean_watch_item("confluence", "123456") == "123456"
    assert clean_watch_item("confluence", "docs") == "DOCS"


def test_resolve_channel_ref_reads_slack_links() -> None:
    from ragtone.channel_preview import resolve_channel_ref, slack_plain

    assert (
        resolve_channel_ref("https://stone.slack.com/archives/C024BE7LT")
        == "C024BE7LT"
    )
    assert (
        resolve_channel_ref(
            "https://stone.slack.com/archives/C024BE7LT/p1710000000000100"
        )
        == "C024BE7LT"
    )
    assert (
        resolve_channel_ref("https://app.slack.com/client/T12345678/C024BE7LT")
        == "C024BE7LT"
    )
    assert resolve_channel_ref("slack://channel?team=T123&id=C024BE7LT") == "C024BE7LT"
    assert (
        resolve_channel_ref("<https://stone.slack.com/archives/C024BE7LT>")
        == "C024BE7LT"
    )
    assert resolve_channel_ref("#eng") == "eng"
    assert resolve_channel_ref("not a channel!!!") is None
    assert slack_plain("<@U123> see <https://ex.com|runbook>") == "see runbook"


def test_peek_chat_uses_history_and_keeps_real_messages() -> None:
    from ragtone.channel_preview import peek_chat

    class Caller:
        async def call_tool(self, name: str, args: dict) -> dict:
            assert name == "conversations_history"
            assert args["channel_id"] == "C024BE7LT"
            assert "oldest" in args
            return {
                "channel_name": "sso-warroom",
                "messages": [
                    {"text": "<@U1> caiu o sso de novo", "user": "ada", "ts": "2"},
                    {"text": "", "user": "bot", "ts": "1"},
                ],
            }

    settings = Settings(chat={"mcp": "chat"})
    data = asyncio.run(
        peek_chat(
            settings,
            "https://stone.slack.com/archives/C024BE7LT",
            caller=Caller(),
        )
    )
    assert data["ok"] is True
    assert data["id"] == "C024BE7LT"
    assert data["title"] == "sso-warroom"
    assert [item["text"] for item in data["messages"]] == ["caiu o sso de novo"]
    assert data["messages"][0]["author"] == "ada"


def test_peek_endpoint_resolves_link_without_mcp(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/admin/peek",
        json={"name": "chat", "ref": "https://stone.slack.com/archives/C024BE7LT"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "C024BE7LT"
    assert data["ok"] is False
    assert data["messages"] == []
    assert "MCP" in data["error"]


def test_peek_endpoint_rejects_unknown_text(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/admin/peek",
        json={"name": "chat", "ref": "??? not a channel"},
    )
    assert response.status_code == 400
    assert response.json()["id"] is None


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
        cloud_id="https://example.atlassian.net",
        backfill_days=30,
        pause=0,
    )
    result = asyncio.run(connector.fetch(None, backfill=True))
    assert result.chunks == []


def test_confluence_skips_mcp_when_no_docs() -> None:
    connector = ConfluenceConnector(
        ConfluenceSource(enabled=True, docs=[]),
        _BoomCaller(),
        cloud_id="https://example.atlassian.net",
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


def test_parse_cutoff_accepts_iso_date() -> None:
    assert parse_cutoff("2026-01-15") == "2026-01-15"
    assert parse_cutoff("2026-01-15T12:00:00Z") == "2026-01-15"
    assert parse_cutoff("") is None
    try:
        parse_cutoff("15/01/2026")
    except ValueError as exc:
        assert "cutoff" in str(exc)
    else:
        raise AssertionError("expected invalid cutoff")


def test_resolve_jira_and_confluence_links() -> None:
    from ragtone.watch_preview import resolve_confluence_ref, resolve_jira_ref

    assert resolve_jira_ref("https://stone.atlassian.net/browse/PAY-12") == "PAY"
    assert (
        resolve_jira_ref("https://stone.atlassian.net/jira/software/c/projects/PAY/boards/9")
        == "PAY"
    )
    assert resolve_jira_ref("PAY-99") == "PAY"
    assert resolve_jira_ref("abc") == "ABC"
    assert resolve_jira_ref("???") is None
    assert (
        resolve_confluence_ref(
            "https://stone.atlassian.net/wiki/spaces/ENG/pages/123456/SSO+runbook"
        )
        == "123456"
    )
    assert resolve_confluence_ref("https://stone.atlassian.net/wiki/spaces/ENG") == "ENG"
    assert resolve_confluence_ref("viewpage.action?pageId=42") == "42"


def test_peek_jira_lists_recent_issues() -> None:
    from ragtone.watch_preview import peek_jira

    class Caller:
        async def call_tool(self, name: str, args: dict) -> dict:
            assert name == "jira_search"
            assert "project = PAY" in args["jql"]
            return {
                "issues": [
                    {
                        "key": "PAY-1",
                        "fields": {
                            "summary": "login caiu",
                            "updated": "2026-09-01",
                            "project": {"name": "Pagamentos", "key": "PAY"},
                        },
                    }
                ]
            }

    data = asyncio.run(
        peek_jira(Settings(jira={"mcp": "atlassian"}), "PAY-1", caller=Caller())
    )
    assert data["ok"] is True
    assert data["id"] == "PAY"
    assert data["title"] == "Pagamentos"
    assert data["kind"] == "project"
    assert data["items"][0]["text"] == "login caiu"
    assert data["items"][0]["author"] == "PAY-1"


def test_peek_confluence_page_and_space() -> None:
    from ragtone.watch_preview import peek_confluence

    class PageCaller:
        async def call_tool(self, name: str, args: dict) -> dict:
            assert name == "confluence_get_page"
            assert args["pageId"] == "123456"
            return {
                "id": "123456",
                "title": "SSO runbook",
                "body": "como resetar o sso",
                "lastModified": "2026-09-01",
            }

    page = asyncio.run(
        peek_confluence(Settings(confluence={"mcp": "atlassian"}), "123456", caller=PageCaller())
    )
    assert page["ok"] is True
    assert page["kind"] == "page"
    assert page["title"] == "SSO runbook"
    assert "resetar" in page["items"][0]["text"]

    class SpaceCaller:
        async def call_tool(self, name: str, args: dict) -> dict:
            assert name == "confluence_search"
            assert "space = ENG" in args["cql"]
            return {
                "results": [
                    {
                        "id": "1",
                        "title": "Intro",
                        "excerpt": "bem-vindo ao espaço",
                        "lastModified": "2026-09-01",
                    }
                ]
            }

    space = asyncio.run(
        peek_confluence(Settings(confluence={"mcp": "atlassian"}), "ENG", caller=SpaceCaller())
    )
    assert space["kind"] == "space"
    assert space["items"][0]["author"] == "Intro"


def test_peek_endpoint_resolves_jira_link_without_mcp(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/admin/peek",
        json={"name": "jira", "ref": "https://stone.atlassian.net/browse/PAY-12"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "PAY"
    assert data["ok"] is False
    assert data["items"] == []
    assert "MCP" in data["error"]


def test_overlay_settings_keeps_cutoffs() -> None:
    settings = Settings()
    next_ = overlay_settings(
        settings,
        {
            "jira": [{"id": "ABC", "cutoff": "2026-01-15"}],
            "confluence": [{"id": "42", "cutoff": "2026-02-01"}],
            "chat": [{"id": "eng", "cutoff": "2026-03-01"}],
        },
    )
    assert next_.jira.project_cutoffs == {"ABC": "2026-01-15"}
    assert next_.confluence.doc_cutoffs == {"42": "2026-02-01"}
    assert next_.chat.channel_cutoffs == {"eng": "2026-03-01"}


def test_board_http_saves_cutoff_for_jira(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    saved = client.post(
        "/api/admin/watches",
        json={"name": "jira", "items": [{"id": "abc", "cutoff": "2026-01-15"}]},
    )
    assert saved.status_code == 200
    jira = next(row for row in saved.json()["connectors"] if row["name"] == "jira")
    assert jira["targets"][0]["cutoff"] == "2026-01-15"
    stored = (tmp_path / "watches.json").read_text()
    assert "2026-01-15" in stored


def test_board_http_rejects_invalid_cutoff(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/admin/watches",
        json={"name": "chat", "items": [{"id": "eng", "cutoff": "ontem"}]},
    )
    assert response.status_code == 400


def test_jira_uses_project_cutoff_until_checkpoint(tmp_path: Path) -> None:
    from ragtone.checkpoints import CheckpointStore
    from ragtone.ingest.jira import JiraConnector

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call_tool(self, name: str, args: dict) -> dict:
            self.calls.append((name, args))
            return {"issues": [], "isLast": True}

    caller = Recorder()
    checkpoints = CheckpointStore(tmp_path / "checkpoints.json")
    connector = JiraConnector(
        JiraSource(
            enabled=True,
            projects=["ABC"],
            project_cutoffs={"ABC": "2026-01-15"},
        ),
        caller,
        cloud_id="https://example.atlassian.net",
        backfill_days=365,
        pause=0,
        checkpoints=checkpoints,
    )
    asyncio.run(connector.fetch(None, backfill=False))
    assert "2026-01-15" in caller.calls[0][1]["jql"]
    checkpoints.set("jira:ABC", "2026-06-01")
    asyncio.run(connector.fetch(None, backfill=False))
    assert "2026-06-01" in caller.calls[-1][1]["jql"]
    asyncio.run(connector.fetch(None, backfill=True))
    assert "2026-01-15" in caller.calls[-1][1]["jql"]


def test_chat_uses_cutoff_date() -> None:
    from ragtone.ingest.chat import ChatConnector
    from ragtone.ingest.parse import unix_from_iso_date

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call_tool(self, name: str, args: dict) -> dict:
            self.calls.append((name, args))
            return {"messages": []}

    caller = Recorder()
    connector = ChatConnector(
        ChatSource(enabled=True, channels=["eng"], channel_cutoffs={"eng": "2026-01-15"}),
        caller,
        pause=0,
        default_days=365,
    )
    asyncio.run(connector.fetch(None, backfill=False))
    assert caller.calls[0][1]["oldest"] == unix_from_iso_date("2026-01-15")


def test_jira_projects_keep_separate_cutoffs() -> None:
    from ragtone.ingest.jira import JiraConnector

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call_tool(self, name: str, args: dict) -> dict:
            self.calls.append((name, args))
            return {"issues": [], "isLast": True}

    caller = Recorder()
    connector = JiraConnector(
        JiraSource(
            enabled=True,
            projects=["ABC", "PLAT"],
            project_cutoffs={"ABC": "2026-01-15", "PLAT": "2026-06-01"},
        ),
        caller,
        cloud_id="https://example.atlassian.net",
        backfill_days=365,
        pause=0,
    )
    asyncio.run(connector.fetch(None, backfill=False))
    jqls = [args["jql"] for name, args in caller.calls]
    assert any("project in (ABC)" in item and "2026-01-15" in item for item in jqls)
    assert any("project in (PLAT)" in item and "2026-06-01" in item for item in jqls)
