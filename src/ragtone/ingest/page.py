from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from ragtone.ingest.client import ToolCaller
from ragtone.ingest.parse import as_records

log = logging.getLogger(__name__)

MAX_PAGES = 1000
NextArgs = Callable[[Any, list[dict[str, Any]], dict[str, Any]], dict[str, Any] | None]


@dataclass
class SearchPage:
    records: list[dict[str, Any]]
    next_args: dict[str, Any] | None
    total: int | None
    payload: Any


def _total(payload: Any) -> int | None:
    if not isinstance(payload, dict) or payload.get("total") is None:
        return None
    try:
        return int(payload["total"])
    except (TypeError, ValueError):
        return None


async def search_page(
    caller: ToolCaller,
    tool: str,
    base: dict[str, Any],
    *keys: str,
    next_args: NextArgs,
    extra: dict[str, Any] | None = None,
) -> SearchPage:
    args = {**base, **(extra or {})}
    payload = await caller.call_tool(tool, args)
    records = as_records(payload, *keys)
    nxt = next_args(payload, records, args)
    return SearchPage(records=records, next_args=nxt, total=_total(payload), payload=payload)


async def paged_records(
    caller: ToolCaller,
    tool: str,
    base: dict[str, Any],
    *keys: str,
    pause: float = 0,
    next_args: NextArgs,
    max_pages: int = MAX_PAGES,
    record_parser: Callable[[Any], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    extra: dict[str, Any] | None = None
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    pages = 0
    for _ in range(max_pages):
        page = await search_page(caller, tool, base, *keys, next_args=next_args, extra=extra)
        if record_parser is not None:
            parsed = record_parser(page.payload)
            if parsed:
                page.records = parsed
        out.extend(page.records)
        pages += 1
        if not page.next_args:
            break
        token = json.dumps(page.next_args, sort_keys=True)
        if token in seen:
            break
        seen.add(token)
        extra = page.next_args
        if pause:
            await asyncio.sleep(pause)
    if pages > 1:
        log.info("%s: %s records across %s pages", tool, len(out), pages)
    return out


def _cursor(payload: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if value:
            return str(value)
    meta = payload.get("response_metadata")
    if isinstance(meta, dict):
        for name in names:
            value = meta.get(name)
            if value:
                return str(value)
    return None


def _oldest_ts(records: list[dict[str, Any]]) -> str | None:
    stamps = [
        str(item.get("ts") or item.get("id") or "")
        for item in records
        if item.get("ts") or item.get("id")
    ]
    stamps = [item for item in stamps if item]
    return min(stamps) if stamps else None


def chat_next(
    payload: Any,
    records: list[dict[str, Any]],
    request: dict[str, Any],
) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        if payload.get("has_more") is False:
            return None
        cursor = _cursor(payload, "next_cursor", "cursor")
        if cursor:
            return {"cursor": cursor}
        if payload.get("has_more"):
            latest = _oldest_ts(records)
            if latest:
                return {"latest": latest}
        return None
    limit = int(request.get("limit") or 0)
    if limit and len(records) >= limit:
        latest = _oldest_ts(records)
        if latest:
            return {"latest": latest}
    return None


def jira_next(
    payload: Any,
    records: list[dict[str, Any]],
    request: dict[str, Any],
) -> dict[str, Any] | None:
    limit = int(request.get("limit") or request.get("maxResults") or 50)
    if isinstance(payload, dict):
        if payload.get("isLast") is True:
            return None
        page_info = payload.get("pageInfo") or payload.get("page_info")
        if isinstance(page_info, dict):
            if page_info.get("hasNextPage") is False:
                return None
            end = page_info.get("endCursor") or page_info.get("end_cursor")
            if end:
                return {"nextPageToken": str(end)}
        token = _cursor(payload, "nextPageToken", "next_page_token")
        if token:
            return {"nextPageToken": token}
        total = payload.get("total")
        start = int(payload.get("startAt") or request.get("start_at") or request.get("startAt") or 0)
        if total is not None:
            try:
                total_i = int(total)
            except (TypeError, ValueError):
                total_i = -1
            if 0 <= total_i <= start + len(records):
                return None
            if total_i > start + len(records) and records:
                return {"start_at": start + len(records)}
    if len(records) >= limit:
        start = int(request.get("start_at") or request.get("startAt") or 0)
        return {"start_at": start + len(records)}
    return None


def confluence_next(
    payload: Any,
    records: list[dict[str, Any]],
    request: dict[str, Any],
) -> dict[str, Any] | None:
    limit = int(request.get("limit") or 25)
    if isinstance(payload, dict):
        if payload.get("has_more") is False:
            return None
        cursor = _cursor(payload, "cursor", "next_cursor")
        links = payload.get("_links")
        if not cursor and isinstance(links, dict):
            nxt = links.get("next")
            if isinstance(nxt, str):
                query = parse_qs(urlparse(nxt).query)
                if query.get("cursor"):
                    return {"cursor": query["cursor"][0]}
                if query.get("start"):
                    return {"start": int(query["start"][0])}
        if cursor:
            return {"cursor": cursor}
        start = int(payload.get("start") or request.get("start") or 0)
        total = payload.get("totalSize") or payload.get("total")
        if total is not None:
            try:
                total_i = int(total)
            except (TypeError, ValueError):
                total_i = -1
            if 0 <= total_i <= start + len(records):
                return None
            if total_i > start + len(records) and records:
                return {"start": start + len(records)}
    if len(records) >= limit:
        start = int(request.get("start") or 0)
        return {"start": start + len(records)}
    return None
