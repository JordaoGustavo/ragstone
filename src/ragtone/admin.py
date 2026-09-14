from __future__ import annotations

from typing import Any

from ragtone.settings import Settings
from ragtone.watches import parse_targets, yaml_watches

WATCH_KIND = {
    "jira": "projects",
    "confluence": "docs",
    "chat": "channels",
}


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
    watches: dict[str, list] | None = None,
) -> list[dict[str, Any]]:
    specs = {
        "jira": settings.jira,
        "confluence": settings.confluence,
        "chat": settings.chat,
    }
    watching = watches or yaml_watches(settings)
    by_source = stats.get("by_source") or {}
    rows: list[dict[str, Any]] = []
    for name, spec in specs.items():
        configured = mcp_is_configured(settings, spec.mcp)
        targets = parse_targets(name, watching.get(name) or [])
        ids = [item["id"] for item in targets]
        checkpoint = checkpoints.get(name)
        if name == "chat":
            stamps = [
                value
                for key, value in checkpoints.items()
                if key == "chat" or str(key).startswith("chat:")
            ]
            if stamps:
                checkpoint = max(stamps)
        row: dict[str, Any] = {
            "name": name,
            "enabled": spec.enabled,
            "mcp": spec.mcp,
            "mcp_configured": configured,
            "checkpoint": checkpoint,
            "chunks": int(by_source.get(name, 0)),
            "watching": ids,
            "targets": targets,
            "watch_kind": WATCH_KIND[name],
            "can_sync": bool(spec.enabled and configured and ids),
        }
        if name == "chat":
            row["channels"] = ids
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
    empty_codes = {
        "jira": "jira_no_boards",
        "confluence": "confluence_no_docs",
        "chat": "chat_no_channels",
    }
    for row in connectors:
        name = str(row["name"])
        if not row["enabled"]:
            gaps.append({"code": "disabled", "connector": name})
            continue
        if not row["mcp_configured"]:
            gaps.append({"code": "mcp_missing", "connector": name})
        if not row.get("watching"):
            gaps.append({"code": empty_codes[name], "connector": name})
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
    watches: dict[str, list] | None = None,
) -> dict[str, Any]:
    connectors = connector_rows(settings, stats, checkpoints, watches)
    watching = watches or yaml_watches(settings)
    return {
        "index": {
            "ok": bool(stats.get("ok")),
            "total": int(stats.get("total") or 0),
            "by_source": dict(stats.get("by_source") or {}),
        },
        "gaps": collect_gaps(settings, stats, connectors),
        "connectors": connectors,
        "watches": {
            name: [item["id"] for item in parse_targets(name, watching.get(name) or [])]
            for name in WATCH_KIND
        },
        "recent": recent,
        "job": job,
    }
