from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ragtone.models import Chunk


@dataclass
class FetchResult:
    chunks: list[Chunk] = field(default_factory=list)
    watermark: str | None = None


class Connector(Protocol):
    name: str

    async def fetch(self, checkpoint: str | None, *, backfill: bool) -> FetchResult: ...
