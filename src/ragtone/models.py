from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Chunk:
    source: str
    native_id: str
    text: str
    title: str = ""
    url: str = ""
    parent_id: str = ""
    thread_id: str = ""
    channel_or_space: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    authors: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return f"{self.source}:{self.native_id}"

    def to_document(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["id"] = self.id
        payload["authors"] = list(self.authors)
        return payload


@dataclass(frozen=True)
class Filters:
    source: str | None = None
    channel: str | None = None
    since: str | None = None


@dataclass(frozen=True)
class Hit:
    id: str
    score: float
    source: str
    title: str
    text: str
    url: str
    native_id: str
    parent_id: str = ""
    thread_id: str = ""
    channel_or_space: str = ""
    updated_at: str | None = None

    def as_dict(self, *, text_limit: int | None = None) -> dict[str, Any]:
        data = asdict(self)
        if text_limit is not None and len(data["text"]) > text_limit:
            data["text"] = data["text"][:text_limit] + "…"
        return data
