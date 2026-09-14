from __future__ import annotations

from typing import Any

from ragtone.settings import Settings


def idle_job() -> dict[str, Any]:
    return {
        "status": "idle",
        "connector": None,
        "backfill": False,
        "chunks": 0,
        "error": None,
    }


def mcp_is_configured(settings: Settings, mcp_name: str) -> bool:
    try:
        spec = settings.mcp_by_name(mcp_name)
    except KeyError:
        return False
    if spec.transport == "stdio":
        return bool(spec.command)
    url = (spec.url or "").strip()
    return bool(url) and "replace-with" not in url


def connector_rows(
    settings: Settings,
    stats: dict[str, Any],
    checkpoints: dict[str, str],
) -> list[dict[str, Any]]:
    specs = {
        "jira": settings.jira,
        "confluence": settings.confluence,
        "chat": settings.chat,
    }
    by_source = stats.get("by_source") or {}
    rows: list[dict[str, Any]] = []
    for name, spec in specs.items():
        configured = mcp_is_configured(settings, spec.mcp)
        row: dict[str, Any] = {
            "name": name,
            "enabled": spec.enabled,
            "mcp": spec.mcp,
            "mcp_configured": configured,
            "checkpoint": checkpoints.get(name),
            "chunks": int(by_source.get(name, 0)),
            "can_sync": bool(spec.enabled and configured),
        }
        if name == "chat":
            row["channels"] = list(settings.chat.channels)
        rows.append(row)
    return rows


def collect_gaps(
    settings: Settings,
    stats: dict[str, Any],
    connectors: list[dict[str, Any]],
) -> list[dict[str, str | None]]:
    gaps: list[dict[str, str | None]] = []
    if not stats.get("ok"):
        gaps.append({"code": "es_down", "connector": None})
    for row in connectors:
        name = str(row["name"])
        if not row["enabled"]:
            gaps.append({"code": "disabled", "connector": name})
            continue
        if not row["mcp_configured"]:
            gaps.append({"code": "mcp_missing", "connector": name})
        if name == "chat" and not settings.chat.channels:
            gaps.append({"code": "chat_no_channels", "connector": "chat"})
        if not row.get("checkpoint"):
            gaps.append({"code": "never_ran", "connector": name})
        if stats.get("ok") and int(row.get("chunks") or 0) == 0:
            gaps.append({"code": "zero_chunks", "connector": name})
    return gaps


def snapshot(
    settings: Settings,
    *,
    stats: dict[str, Any],
    checkpoints: dict[str, str],
    recent: list[dict[str, Any]],
    job: dict[str, Any],
) -> dict[str, Any]:
    connectors = connector_rows(settings, stats, checkpoints)
    return {
        "index": {
            "ok": bool(stats.get("ok")),
            "total": int(stats.get("total") or 0),
            "by_source": dict(stats.get("by_source") or {}),
        },
        "gaps": collect_gaps(settings, stats, connectors),
        "connectors": connectors,
        "recent": recent,
        "job": job,
    }
