from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ragtone.settings import Settings

SOURCES = ("jira", "confluence", "chat")
_CHAT = re.compile(r"[A-Za-z0-9._-]{1,80}$")
_JIRA = re.compile(r"(?:[A-Za-z][A-Za-z0-9_]{0,31}|[0-9]{1,12})$")
_CONFLUENCE = re.compile(r"(?:~?[A-Za-z][A-Za-z0-9._-]{0,31}|[0-9]{1,12})$")
_CUTOFF = re.compile(r"^\d{4}-\d{2}-\d{2}")


def clean_watch_item(source: str, raw: str) -> str | None:
    text = str(raw).strip()
    if source == "chat":
        text = text.lstrip("#")
        if not _CHAT.fullmatch(text):
            return None
        return text
    if source == "jira":
        if not _JIRA.fullmatch(text):
            return None
        return text.upper() if any(ch.isalpha() for ch in text) else text
    if source == "confluence":
        if not _CONFLUENCE.fullmatch(text):
            return None
        return text if text.isdigit() else text.upper()
    return None


def parse_backfill_days(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        days = int(raw)
    except (TypeError, ValueError):
        raise ValueError("backfill_days inválido") from None
    if days < 0 or days > 3650:
        raise ValueError("backfill_days inválido")
    return days


def parse_cutoff(raw: object) -> str | None:
    if raw is None or raw == "":
        return None
    text = str(raw).strip()
    if not _CUTOFF.match(text):
        raise ValueError("cutoff inválido")
    try:
        parsed = datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("cutoff inválido") from None
    if parsed.year < 1970 or parsed.year > 2100:
        raise ValueError("cutoff inválido")
    return parsed.isoformat()


def _as_target(source: str, raw: object) -> dict[str, Any]:
    if isinstance(raw, str):
        ident = clean_watch_item(source, raw)
        if ident is None:
            raise ValueError(f"item inválido: {raw}")
        return {"id": ident, "backfill_days": None, "cutoff": None}
    if isinstance(raw, dict):
        ident = clean_watch_item(source, str(raw.get("id") or raw.get("name") or ""))
        if ident is None:
            raise ValueError(f"item inválido: {raw}")
        return {
            "id": ident,
            "backfill_days": parse_backfill_days(raw.get("backfill_days")),
            "cutoff": parse_cutoff(raw.get("cutoff")),
        }
    raise ValueError(f"item inválido: {raw}")


def parse_targets(source: str, raw_items: object) -> list[dict[str, Any]]:
    if source not in SOURCES:
        raise ValueError("conector desconhecido")
    if not isinstance(raw_items, list):
        raise ValueError("items precisa ser uma lista")
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_items:
        target = _as_target(source, item)
        key = str(target["id"]).casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(target)
    return cleaned


def parse_items(source: str, raw_items: object) -> list[str]:
    return [str(item["id"]) for item in parse_targets(source, raw_items)]


def yaml_watches(settings: Settings) -> dict[str, list[dict[str, Any]]]:
    empty = {"backfill_days": None, "cutoff": None}
    return {
        "jira": [{"id": item, **empty} for item in settings.jira.projects],
        "confluence": [{"id": item, **empty} for item in settings.confluence.docs],
        "chat": [{"id": item, **empty} for item in settings.chat.channels],
    }


def _cutoffs(targets: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(item["id"]): str(item["cutoff"])
        for item in targets
        if item.get("cutoff")
    }


def overlay_settings(settings: Settings, watching: dict[str, list]) -> Settings:
    copy = settings.model_copy(deep=True)
    jira = parse_targets("jira", watching.get("jira") or [])
    confluence = parse_targets("confluence", watching.get("confluence") or [])
    chat = parse_targets("chat", watching.get("chat") or [])
    copy.jira.projects = [item["id"] for item in jira]
    copy.jira.project_cutoffs = _cutoffs(jira)
    copy.confluence.docs = [item["id"] for item in confluence]
    copy.confluence.doc_cutoffs = _cutoffs(confluence)
    copy.chat.channels = [item["id"] for item in chat]
    copy.chat.channel_windows = {
        str(item["id"]): int(item["backfill_days"])
        for item in chat
        if item.get("backfill_days") is not None
    }
    copy.chat.channel_cutoffs = _cutoffs(chat)
    return copy


def _dump_targets(targets: list[dict[str, Any]]) -> list:
    dumped: list = []
    for item in targets:
        extra: dict[str, Any] = {}
        if item.get("cutoff"):
            extra["cutoff"] = item["cutoff"]
        if item.get("backfill_days") is not None:
            extra["backfill_days"] = item["backfill_days"]
        if extra:
            dumped.append({"id": item["id"], **extra})
        else:
            dumped.append(item["id"])
    return dumped


class WatchStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, list[dict[str, Any]] | None] = {name: None for name in SOURCES}
        if path.exists():
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                for name in SOURCES:
                    value = raw.get(name)
                    if isinstance(value, list):
                        try:
                            self._data[name] = parse_targets(name, value)
                        except ValueError:
                            self._data[name] = None

    def resolved(self, settings: Settings) -> dict[str, list[dict[str, Any]]]:
        defaults = yaml_watches(settings)
        return {
            name: list(self._data[name]) if self._data[name] is not None else defaults[name]
            for name in SOURCES
        }

    def set(self, name: str, items: list) -> None:
        if name not in SOURCES:
            raise ValueError("conector desconhecido")
        self._data[name] = parse_targets(name, items)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: _dump_targets(value)
            for key, value in self._data.items()
            if value is not None
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
