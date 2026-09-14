from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (as_text(item) for item in value) if part)
    if isinstance(value, dict):
        if value.get("type") == "text":
            return str(value.get("text") or "")
        if "content" in value:
            return as_text(value.get("content"))
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def as_records(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            nested = payload.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return [payload]
    return []


def iso_days_ago(days: int) -> str:
    start = datetime.now(timezone.utc) - timedelta(days=days)
    return start.date().isoformat()


def unix_days_ago(days: int) -> str:
    if days <= 0:
        return "0"
    start = datetime.now(timezone.utc) - timedelta(days=days)
    return f"{start.timestamp():.6f}"


def later_watermark(*values: str | None) -> str | None:
    stamps = [value for value in values if value]
    if not stamps:
        return None
    return max(stamps)
