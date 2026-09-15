from __future__ import annotations

import re
from typing import Any

from ragtone.admin import mcp_is_configured
from ragtone.channel_preview import (
    PREVIEW_K,
    TEXT_LIMIT,
    peek_chat,
    resolve_channel_ref,
)
from ragtone.ingest.client import FoundationClient, ToolCaller
from ragtone.ingest.parse import as_records, as_text
from ragtone.settings import Settings
from ragtone.watches import SOURCES, clean_watch_item

_WRAPPED_URL = re.compile(r"^<(https?://[^|>]+)(?:\|[^>]*)?>$")
_JIRA_BROWSE = re.compile(r"/browse/([A-Za-z][A-Za-z0-9_]{0,31})-\d+", re.I)
_JIRA_PROJECT = re.compile(r"/projects/([A-Za-z][A-Za-z0-9_]{0,31})", re.I)
_JIRA_QUERY = re.compile(
    r"[?&](?:projectKey|selectedProjectKey|project)=([A-Za-z][A-Za-z0-9_]{0,31})",
    re.I,
)
_JIRA_ISSUE = re.compile(r"^([A-Za-z][A-Za-z0-9_]{0,31})-\d+$")
_CONF_PAGE = re.compile(r"/pages/(\d{1,12})", re.I)
_CONF_PAGE_ID = re.compile(r"(?:pageId|page_id)=(\d{1,12})", re.I)
_CONF_SPACE = re.compile(r"/spaces/([^/?#]+)", re.I)

_MISS = {
    "jira": "não deu para ler um projeto nesse texto — cola o link do Jira",
    "confluence": "não deu para ler um espaço ou página — cola o link do Confluence",
    "chat": "não deu para ler um canal nesse texto — cola o link do Slack",
}


def _unwrap(raw: str) -> str:
    text = str(raw).strip()
    wrapped = _WRAPPED_URL.fullmatch(text)
    return wrapped.group(1) if wrapped else text


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}…"


def resolve_jira_ref(raw: str) -> str | None:
    text = _unwrap(raw)
    for pattern in (_JIRA_BROWSE, _JIRA_PROJECT, _JIRA_QUERY):
        match = pattern.search(text)
        if match:
            return clean_watch_item("jira", match.group(1))
    issue = _JIRA_ISSUE.fullmatch(text.strip())
    if issue:
        return clean_watch_item("jira", issue.group(1))
    return clean_watch_item("jira", text)


def resolve_confluence_ref(raw: str) -> str | None:
    text = _unwrap(raw)
    for pattern in (_CONF_PAGE, _CONF_PAGE_ID):
        match = pattern.search(text)
        if match:
            return clean_watch_item("confluence", match.group(1))
    space = _CONF_SPACE.search(text)
    if space:
        ident = space.group(1)
        if ident.lower() not in {"pages", "overview", "blog"}:
            return clean_watch_item("confluence", ident)
    return clean_watch_item("confluence", text)


def resolve_watch_ref(source: str, raw: str) -> str | None:
    if source == "chat":
        return resolve_channel_ref(raw)
    if source == "jira":
        return resolve_jira_ref(raw)
    if source == "confluence":
        return resolve_confluence_ref(raw)
    return None


def _fields(record: dict[str, Any]) -> dict[str, Any]:
    nested = record.get("fields")
    return nested if isinstance(nested, dict) else record


def _issue_item(issue: dict[str, Any]) -> dict[str, str] | None:
    fields = _fields(issue)
    key = str(issue.get("key") or fields.get("key") or "")
    summary = as_text(fields.get("summary") or issue.get("summary"))
    if not key and not summary:
        return None
    return {
        "text": _clip(summary or key),
        "author": key,
        "ts": str(fields.get("updated") or issue.get("updated") or ""),
    }


def _page_item(page: dict[str, Any]) -> dict[str, str] | None:
    nested = page.get("content")
    content = nested if isinstance(nested, dict) else page
    title = as_text(content.get("title") or page.get("title"))
    raw_body = page.get("excerpt") or page.get("body") or page.get("text")
    if raw_body is None and not isinstance(nested, dict):
        raw_body = page.get("content")
    if raw_body is None:
        raw_body = content.get("body")
    body = as_text(raw_body)
    text = body or title
    if not text:
        return None
    return {
        "text": _clip(text),
        "author": title or str(content.get("id") or page.get("id") or ""),
        "ts": str(
            page.get("lastModified")
            or page.get("updated")
            or content.get("lastModified")
            or ""
        ),
    }


def _jira_title(payload: Any, project: str) -> str:
    for issue in as_records(payload, "issues", "results", "values"):
        fields = _fields(issue)
        nested = fields.get("project")
        if isinstance(nested, dict):
            name = nested.get("name") or nested.get("key")
            if name:
                return str(name).strip()
    return project


def _page_title(payload: Any, ident: str) -> str:
    if isinstance(payload, dict):
        title = as_text(payload.get("title"))
        if title:
            return title
        content = payload.get("content")
        if isinstance(content, dict):
            nested = as_text(content.get("title"))
            if nested:
                return nested
    return ident


def _empty(ident: str | None, *, error: str | None = None, kind: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "id": ident,
        "title": ident,
        "kind": kind,
        "items": [],
        "messages": [],
        "error": error,
    }


async def _with_caller(
    settings: Settings,
    mcp_name: str,
    caller: ToolCaller | None,
    run,
):
    if caller is not None:
        return await run(caller)
    spec = settings.mcp_by_name(mcp_name)
    async with FoundationClient(spec) as client:
        return await run(client)


async def peek_jira(
    settings: Settings,
    raw: str,
    *,
    caller: ToolCaller | None = None,
    k: int = PREVIEW_K,
) -> dict[str, Any]:
    project = resolve_jira_ref(raw)
    if not project:
        return _empty(None, error=_MISS["jira"], kind="project")
    empty = _empty(project, kind="project")
    if caller is None and not mcp_is_configured(settings, settings.jira.mcp):
        return {**empty, "error": "MCP jira faltando — o id saiu do link, mas não deu para puxar issues"}

    async def _run(active: ToolCaller) -> dict[str, Any]:
        args: dict[str, Any] = {
            "jql": f'project = {project} ORDER BY updated DESC',
            "maxResults": k,
        }
        if settings.atlassian_cloud_id:
            args["cloudId"] = settings.atlassian_cloud_id
        payload = await active.call_tool(settings.jira.search_tool, args)
        items: list[dict[str, str]] = []
        for issue in as_records(payload, "issues", "results", "values"):
            item = _issue_item(issue)
            if item:
                items.append(item)
            if len(items) >= k:
                break
        title = _jira_title(payload, project)
        if not items:
            return {
                "ok": True,
                "id": project,
                "title": title,
                "kind": "project",
                "items": [],
                "messages": [],
                "error": "nenhuma issue recente nesse projeto",
            }
        return {
            "ok": True,
            "id": project,
            "title": title,
            "kind": "project",
            "items": items,
            "messages": items,
            "error": None,
        }

    try:
        return await _with_caller(settings, settings.jira.mcp, caller, _run)
    except Exception as exc:
        return {**empty, "error": str(exc) or "não deu para olhar o projeto"}


async def peek_confluence(
    settings: Settings,
    raw: str,
    *,
    caller: ToolCaller | None = None,
    k: int = PREVIEW_K,
) -> dict[str, Any]:
    ident = resolve_confluence_ref(raw)
    if not ident:
        return _empty(None, error=_MISS["confluence"])
    kind = "page" if ident.isdigit() else "space"
    empty = _empty(ident, kind=kind)
    if caller is None and not mcp_is_configured(settings, settings.confluence.mcp):
        return {
            **empty,
            "error": "MCP confluence faltando — o id saiu do link, mas não deu para puxar o doc",
        }

    async def _run(active: ToolCaller) -> dict[str, Any]:
        payload: Any = {}
        items: list[dict[str, str]] = []
        title = ident
        if kind == "page" and settings.confluence.get_tool:
            fetched = await active.call_tool(
                settings.confluence.get_tool,
                {
                    "pageId": ident,
                    **(
                        {"cloudId": settings.atlassian_cloud_id}
                        if settings.atlassian_cloud_id
                        else {}
                    ),
                },
            )
            payload = fetched
            title = _page_title(fetched, ident)
            if isinstance(fetched, dict):
                item = _page_item(fetched)
                if item:
                    items.append(item)
        if not items:
            cql = f"id = {ident}" if kind == "page" else f"space = {ident} ORDER BY lastModified DESC"
            args: dict[str, Any] = {"cql": cql, "limit": k}
            if settings.atlassian_cloud_id:
                args["cloudId"] = settings.atlassian_cloud_id
            payload = await active.call_tool(settings.confluence.search_tool, args)
            title = _page_title(payload, ident) if kind == "page" else ident
            for page in as_records(payload, "results", "pages", "values"):
                item = _page_item(page)
                if item:
                    items.append(item)
                if len(items) >= k:
                    break
            if kind == "space" and not title:
                title = ident
        if not items:
            return {
                "ok": True,
                "id": ident,
                "title": title,
                "kind": kind,
                "items": [],
                "messages": [],
                "error": "nada recente nesse doc",
            }
        return {
            "ok": True,
            "id": ident,
            "title": title,
            "kind": kind,
            "items": items,
            "messages": items,
            "error": None,
        }

    try:
        return await _with_caller(settings, settings.confluence.mcp, caller, _run)
    except Exception as exc:
        return {**empty, "error": str(exc) or "não deu para olhar o doc"}


async def peek_source(
    settings: Settings,
    name: str,
    raw: str,
    *,
    caller: ToolCaller | None = None,
) -> dict[str, Any]:
    if name not in SOURCES:
        return _empty(None, error="conector desconhecido")
    if name == "chat":
        data = await peek_chat(settings, raw, caller=caller)
        items = list(data.get("messages") or [])
        return {**data, "kind": "channel", "items": items, "messages": items}
    if name == "jira":
        return await peek_jira(settings, raw, caller=caller)
    return await peek_confluence(settings, raw, caller=caller)
