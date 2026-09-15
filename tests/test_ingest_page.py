from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from ragtone.ingest.chat import ChatConnector
from ragtone.ingest.confluence import ConfluenceConnector
from ragtone.ingest.jira import JiraConnector
from ragtone.ingest.parse import as_records, iso_days_ago
from ragtone.settings import ChatSource, ConfluenceSource, JiraSource


class _Pager:
    def __init__(self, history=None, search=None, replies=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.history = list(history or [])
        self.search = list(search or [])
        self.replies = replies or {"messages": []}

    async def call_tool(self, name: str, args: dict) -> dict:
        self.calls.append((name, dict(args)))
        if name in {"conversations_history", "conversations_replies"}:
            if name == "conversations_replies":
                return dict(self.replies)
            index = 0 if not args.get("cursor") else 1
            return dict(self.history[index])
        index = 0
        if args.get("page_token") or args.get("nextPageToken") or args.get("cursor"):
            index = 1
        elif int(args.get("start_at") or args.get("start") or 0) > 0:
            index = 1
        pages = self.search
        return dict(pages[index])


def test_as_records_unwraps_graphql_nodes() -> None:
    payload = {
        "issues": {
            "nodes": [{"key": "ABC-1"}, {"key": "ABC-2"}],
            "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
        }
    }
    assert [item["key"] for item in as_records(payload, "issues", "results")] == [
        "ABC-1",
        "ABC-2",
    ]


def test_jira_backfill_follows_page_token_past_limit_50() -> None:
    first = {
        "issues": [
            {
                "key": f"ABC-{i}",
                "fields": {"summary": f"s{i}", "updated": "2026-01-01T00:00:00.000+0000"},
            }
            for i in range(50)
        ],
        "nextPageToken": "p2",
        "isLast": False,
    }
    second = {
        "issues": [
            {
                "key": "ABC-99",
                "fields": {"summary": "tail", "updated": "2026-02-01T00:00:00.000+0000"},
            }
        ],
        "isLast": True,
    }
    caller = _Pager(search=[first, second])
    connector = JiraConnector(
        JiraSource(enabled=True, projects=["ABC"]),
        caller,
        cloud_id="https://example.atlassian.net",
        backfill_days=30,
        pause=0,
    )
    result = asyncio.run(connector.fetch(None, backfill=True))
    keys = [chunk.native_id for chunk in result.chunks if ":comment:" not in chunk.native_id]
    assert len(keys) == 51
    assert "ABC-99" in keys
    assert result.watermark == "2026-02-01T00:00:00.000+0000"
    jql_calls = [args for name, args in caller.calls if name == "jira_search"]
    assert len(jql_calls) == 2
    assert jql_calls[1].get("nextPageToken") == "p2"


def test_jira_backfill_follows_start_at_when_total_outgrows_the_page() -> None:
    first = {
        "issues": [
            {
                "key": f"ABC-{i}",
                "fields": {"summary": f"s{i}", "updated": "2026-01-01T00:00:00.000+0000"},
            }
            for i in range(50)
        ],
        "startAt": 0,
        "maxResults": 50,
        "total": 51,
    }
    second = {
        "issues": [
            {
                "key": "ABC-99",
                "fields": {"summary": "tail", "updated": "2026-02-01T00:00:00.000+0000"},
            }
        ],
        "startAt": 50,
        "total": 51,
    }
    caller = _Pager(search=[first, second])
    connector = JiraConnector(
        JiraSource(enabled=True, projects=["ABC"]),
        caller,
        cloud_id="https://example.atlassian.net",
        backfill_days=30,
        pause=0,
    )
    result = asyncio.run(connector.fetch(None, backfill=True))
    keys = [chunk.native_id for chunk in result.chunks if ":comment:" not in chunk.native_id]
    assert "ABC-99" in keys
    jql_calls = [args for name, args in caller.calls if name == "jira_search"]
    assert jql_calls[1].get("start_at") == 50


def test_confluence_backfill_follows_cursor_past_limit_25() -> None:
    first = {
        "results": [
            {"id": str(i), "title": f"p{i}", "body": "x", "lastModified": "2026-01-01"}
            for i in range(25)
        ],
        "cursor": "next-page",
    }
    second = {
        "results": [
            {"id": "99", "title": "tail", "body": "y", "lastModified": "2026-03-01"}
        ]
    }
    caller = _Pager(search=[first, second])
    connector = ConfluenceConnector(
        ConfluenceSource(enabled=True, docs=["ENG"]),
        caller,
        cloud_id="https://example.atlassian.net",
        backfill_days=30,
        pause=0,
    )
    result = asyncio.run(connector.fetch(None, backfill=True))
    pages = {chunk.parent_id for chunk in result.chunks}
    assert len(pages) == 26
    assert "99" in pages
    search_calls = [args for name, args in caller.calls if name == "confluence_search"]
    assert len(search_calls) == 2
    assert search_calls[1].get("cursor") == "next-page"


def test_chat_backfill_follows_history_cursor() -> None:
    first = {
        "messages": [
            {"ts": "1710000100.000000", "text": "new", "user": "ada"},
            {"ts": "1710000090.000000", "text": "mid", "user": "ada"},
        ],
        "has_more": True,
        "response_metadata": {"next_cursor": "c2"},
    }
    second = {
        "messages": [{"ts": "1710000080.000000", "text": "old", "user": "ada"}],
        "has_more": False,
    }
    caller = _Pager(history=[first, second])
    connector = ChatConnector(
        ChatSource(enabled=True, channels=["eng"]),
        caller,
        pause=0,
        default_days=30,
    )
    result = asyncio.run(connector.fetch(None, backfill=True))
    texts = [chunk.text for chunk in result.chunks]
    assert texts == ["new", "mid", "old"]
    history_calls = [args for name, args in caller.calls if name == "conversations_history"]
    assert len(history_calls) == 2
    assert history_calls[1].get("cursor") == "c2"
    assert result.watermarks["chat:eng"] == "1710000100.000000"


def test_chat_backfill_can_target_one_channel() -> None:
    caller = _Pager(history=[{"messages": [], "has_more": False}])
    connector = ChatConnector(
        ChatSource(enabled=True, channels=["eng", "ops"]),
        caller,
        pause=0,
        default_days=30,
    )
    page = asyncio.run(
        connector.next_page(None, backfill=True, cursor=None, targets=["ops"])
    )
    assert page.done is True
    assert caller.calls[0][1]["channel_id"] == "ops"
    assert [name for name, _ in caller.calls] == ["conversations_history"]


def test_jira_backfill_can_target_one_project() -> None:
    caller = _Pager(
        search=[
            {
                "issues": [
                    {
                        "key": "PLAT-1",
                        "fields": {"summary": "s", "updated": "2026-01-01T00:00:00.000+0000"},
                    }
                ],
                "isLast": True,
            }
        ]
    )
    connector = JiraConnector(
        JiraSource(enabled=True, projects=["ABC", "PLAT"]),
        caller,
        cloud_id="https://example.atlassian.net",
        backfill_days=30,
        pause=0,
    )
    page = asyncio.run(
        connector.next_page(None, backfill=True, cursor=None, targets=["PLAT"])
    )
    assert page.done is True
    assert "project in (PLAT)" in caller.calls[0][1]["jql"]
    assert "ABC" not in caller.calls[0][1]["jql"]


def test_iso_days_ago_zero_is_epoch() -> None:
    from ragtone.ingest.parse import unix_from_iso_date, window_stamp

    assert iso_days_ago(0) == "1970-01-01"
    assert unix_from_iso_date("1970-01-01") == "0"
    assert (
        window_stamp(
            keyed=None,
            cutoff="2026-01-15",
            backfill=False,
            backfill_days=None,
            default_days=365,
        )
        == "2026-01-15"
    )


def test_jira_backfill_uses_explicit_days() -> None:
    caller = _Pager(search=[{"issues": [], "isLast": True}])
    connector = JiraConnector(
        JiraSource(enabled=True, projects=["ABC"]),
        caller,
        cloud_id="https://example.atlassian.net",
        backfill_days=365,
        pause=0,
    )
    asyncio.run(connector.next_page(None, backfill=True, cursor=None, backfill_days=7))
    expected = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    assert expected in caller.calls[0][1]["jql"]
    asyncio.run(connector.next_page(None, backfill=True, cursor=None, backfill_days=0))
    assert "1970-01-01" in caller.calls[1][1]["jql"]


def test_chat_backfill_uses_explicit_days_over_channel_window() -> None:
    caller = _Pager(history=[{"messages": [], "has_more": False}])
    connector = ChatConnector(
        ChatSource(enabled=True, channels=["eng"], channel_windows={"eng": 30}),
        caller,
        pause=0,
        default_days=365,
    )
    asyncio.run(connector.next_page(None, backfill=True, cursor=None, backfill_days=7))
    oldest = caller.calls[0][1]["oldest"]
    age = datetime.now(timezone.utc).timestamp() - float(oldest)
    assert 6 * 86400 < age < 8 * 86400


def test_chat_parses_vela_text_history_and_thread_replies() -> None:
    from ragtone.ingest.chat import ChatConnector

    history = {
        "messages": """Channel: #eng (C123)

=== Message from Ada Lovelace <ada@example.com> (U1) at 2026-09-15 09:47:29 -03 ===
Message TS: 1789476449.926349
deploy concluido

=== Message from Grace Hopper <grace@example.com> (U2) at 2026-09-14 13:45:25 -03 ===
Message TS: 1789404325.888169
planning
Thread: 1 replies (latest: 2026-09-14 13:46:03 -03)
"""
    }
    thread = {
        "messages": """=== THREAD PARENT MESSAGE ===
From: Grace Hopper <grace@example.com> (U2)
Time: 2026-09-14 13:45:25 -03
Message TS: 1789404325.888169
planning

=== THREAD REPLIES (1 total) ===

--- Reply 1 of 1 ---
From: Ada Lovelace <ada@example.com> (U1)
Time: 2026-09-14 13:46:03 -03
Message TS: 1789404363.027989
confirmado
"""
    }

    class Caller:
        async def call_tool(self, name: str, args: dict) -> dict:
            return thread if name == "replies" else history

    connector = ChatConnector(
        ChatSource(enabled=True, channels=["C123"], history_tool="history", replies_tool="replies"),
        Caller(),
        pause=0,
        default_days=2,
    )
    page = asyncio.run(connector.next_page(None, backfill=True, cursor=None))
    assert [item.ref for item in page.records[:2]] == ["1789476449.926349", "1789404325.888169"]
    assert page.records[1].payload["message"]["reply_count"] == 1
    thread_item = page.records[-1]
    result = asyncio.run(connector.materialize(thread_item))
    assert [chunk.text for chunk in result.chunks] == ["planning", "confirmado"]
