from __future__ import annotations

import asyncio

from ragtone.ingest.chat import ChatConnector
from ragtone.ingest.confluence import ConfluenceConnector
from ragtone.ingest.jira import JiraConnector
from ragtone.ingest.parse import as_records
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
    assert jql_calls[1].get("page_token") == "p2"


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
