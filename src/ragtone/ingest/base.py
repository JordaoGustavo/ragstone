from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ragtone.ingest.parse import later_watermark
from ragtone.models import Chunk


@dataclass
class FetchResult:
    chunks: list[Chunk] = field(default_factory=list)
    watermark: str | None = None
    watermarks: dict[str, str] = field(default_factory=dict)


@dataclass
class WorkRecord:
    ref: str
    payload: dict[str, Any]
    watermark: str | None = None
    checkpoint_key: str | None = None


@dataclass
class Page:
    records: list[WorkRecord] = field(default_factory=list)
    cursor: dict[str, Any] | None = None
    total: int | None = None
    done: bool = True


class Connector(Protocol):
    name: str

    async def next_page(
        self,
        checkpoint: str | None,
        *,
        backfill: bool,
        cursor: dict[str, Any] | None,
        backfill_days: int | None = None,
    ) -> Page: ...

    async def materialize(self, record: WorkRecord) -> FetchResult: ...


async def fetch_all(
    connector: Connector,
    checkpoint: str | None,
    *,
    backfill: bool,
) -> FetchResult:
    chunks: list[Chunk] = []
    newest = checkpoint
    watermarks: dict[str, str] = {}
    cursor: dict[str, Any] | None = None
    while True:
        page = await connector.next_page(checkpoint, backfill=backfill, cursor=cursor)
        for record in page.records:
            result = await connector.materialize(record)
            chunks.extend(result.chunks)
            newest = later_watermark(newest, result.watermark)
            for key, value in result.watermarks.items():
                merged = later_watermark(watermarks.get(key), value)
                if merged:
                    watermarks[key] = merged
        if page.done:
            break
        cursor = page.cursor
        if cursor is None:
            break
    return FetchResult(chunks=chunks, watermark=newest, watermarks=watermarks)
