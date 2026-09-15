from __future__ import annotations

import re
from typing import Any

from ragtone.admin import mcp_is_configured
from ragtone.ingest.client import FoundationClient, ToolCaller
from ragtone.ingest.parse import as_records, as_text, unix_days_ago
from ragtone.settings import Settings
from ragtone.watches import clean_watch_item

_SLACK_ID = re.compile(r"^[CGD][A-Za-z0-9]{8,}$", re.I)
_WRAPPED_URL = re.compile(r"^<(https?://[^|>]+)(?:\|[^>]*)?>$")
_ARCHIVE = re.compile(
    r"(?:slack\.com/(?:archives|messages)/|slack\.com/client/[^/]+/)([CGD][A-Za-z0-9]{8,})",
    re.I,
)
_QUERY_ID = re.compile(
    r"(?:[?&](?:id|channel)=)([CGD][A-Za-z0-9]{8,})",
    re.I,
)
_SLACK_MRKDWN_USER = re.compile(r"<@[^>]+>")
_SLACK_MRKDWN_LINK = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
_SLACK_MRKDWN_URL = re.compile(r"<(https?://[^>]+)>")

PREVIEW_DAYS = 30
PREVIEW_K = 6
TEXT_LIMIT = 180


def _slack_id(value: str) -> str | None:
    if _SLACK_ID.fullmatch(value):
        return value.upper()
    return None


def resolve_channel_ref(raw: str) -> str | None:
    text = str(raw).strip()
    wrapped = _WRAPPED_URL.fullmatch(text)
    if wrapped:
        text = wrapped.group(1)
    for pattern in (_ARCHIVE, _QUERY_ID):
        match = pattern.search(text)
        if match:
            return match.group(1).upper()
    return _slack_id(text) or clean_watch_item("chat", text)


def slack_plain(text: str) -> str:
    cleaned = _SLACK_MRKDWN_USER.sub("", text)
    cleaned = _SLACK_MRKDWN_LINK.sub(r"\2", cleaned)
    cleaned = _SLACK_MRKDWN_URL.sub(r"\1", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}…"


def _title_from_payload(payload: Any, channel: str) -> str:
    if isinstance(payload, dict):
        for key in ("channel_name", "name"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lstrip("#")
        nested = payload.get("channel")
        if isinstance(nested, dict):
            name = nested.get("name") or nested.get("id")
            if name:
                return str(name).strip().lstrip("#")
        if isinstance(nested, str) and nested.strip() and not _slack_id(nested):
            return nested.strip().lstrip("#")
    return channel


def preview_messages(payload: Any, *, k: int = PREVIEW_K) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for message in as_records(payload, "messages", "results", "history"):
        text = slack_plain(as_text(message.get("text") or message.get("body")))
        if not text:
            continue
        items.append(
            {
                "text": _clip(text),
                "author": slack_plain(
                    as_text(message.get("user") or message.get("username") or message.get("author") or "")
                ),
                "ts": str(message.get("ts") or message.get("created") or ""),
            }
        )
        if len(items) >= k:
            break
    return items


async def peek_chat(
    settings: Settings,
    raw: str,
    *,
    caller: ToolCaller | None = None,
    preview_days: int = PREVIEW_DAYS,
    k: int = PREVIEW_K,
) -> dict[str, Any]:
    channel = resolve_channel_ref(raw)
    if not channel:
        return {
            "ok": False,
            "id": None,
            "title": None,
            "messages": [],
            "error": "não deu para ler um canal nesse texto — cola o link do Slack",
        }
    empty = {
        "ok": False,
        "id": channel,
        "title": channel,
        "messages": [],
    }
    if caller is None and not mcp_is_configured(settings, settings.chat.mcp):
        return {
            **empty,
            "error": "MCP chat faltando — o id saiu do link, mas não deu para puxar mensagens",
        }

    async def _run(active: ToolCaller) -> dict[str, Any]:
        history = await active.call_tool(
            settings.chat.history_tool,
            {"channel_id": channel, "oldest": unix_days_ago(preview_days)},
        )
        messages = preview_messages(history, k=k)
        title = _title_from_payload(history, channel)
        if not messages:
            return {
                "ok": True,
                "id": channel,
                "title": title,
                "messages": [],
                "error": "nenhuma mensagem recente nesse canal",
            }
        return {"ok": True, "id": channel, "title": title, "messages": messages, "error": None}

    try:
        if caller is not None:
            return await _run(caller)
        spec = settings.mcp_by_name(settings.chat.mcp)
        async with FoundationClient(spec) as client:
            return await _run(client)
    except Exception as exc:
        return {**empty, "error": str(exc) or "não deu para olhar o canal"}
