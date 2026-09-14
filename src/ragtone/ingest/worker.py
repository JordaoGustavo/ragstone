from __future__ import annotations

import asyncio
import logging
from typing import Sequence

from ragtone.checkpoints import CheckpointStore
from ragtone.embeddings import Embedder
from ragtone.ingest.base import Connector
from ragtone.ingest.chat import build_chat
from ragtone.ingest.client import ToolCaller
from ragtone.ingest.confluence import build_confluence
from ragtone.ingest.jira import build_jira
from ragtone.retrieval import ChunkStore
from ragtone.settings import Settings

log = logging.getLogger(__name__)


def build_connectors(
    settings: Settings,
    callers: dict[str, ToolCaller],
    checkpoints: CheckpointStore | None = None,
) -> list[Connector]:
    connectors: list[Connector] = []
    for builder in (build_jira, build_confluence):
        connector = builder(settings, callers)
        if connector is not None:
            connectors.append(connector)
    chat = build_chat(settings, callers, checkpoints)
    if chat is not None:
        connectors.append(chat)
    return connectors


class IngestWorker:
    def __init__(
        self,
        store: ChunkStore,
        embedder: Embedder,
        checkpoints: CheckpointStore,
        connectors: Sequence[Connector],
        *,
        poll_seconds: int,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.checkpoints = checkpoints
        self.connectors = list(connectors)
        self.poll_seconds = poll_seconds

    async def backfill(self) -> int:
        return await self._run(backfill=True)

    async def poll_once(self) -> int:
        return await self._run(backfill=False)

    async def poll_named(self, name: str, *, backfill: bool = False) -> int:
        connector = next((item for item in self.connectors if item.name == name), None)
        if connector is None:
            raise KeyError(name)
        return await self.ingest_connector(connector, backfill=backfill)

    async def run_loop(self, stop: asyncio.Event | None = None) -> None:
        while True:
            await self.poll_once()
            if stop is not None and stop.is_set():
                return
            log.info("sync sleeping %ss", self.poll_seconds)
            await asyncio.sleep(self.poll_seconds)

    async def ingest_connector(self, connector: Connector, *, backfill: bool) -> int:
        checkpoint = self.checkpoints.get(connector.name)
        result = await connector.fetch(checkpoint, backfill=backfill)
        count = 0
        if result.chunks:
            vectors = self.embedder.embed([chunk.text for chunk in result.chunks])
            self.store.upsert(result.chunks, vectors)
            count = len(result.chunks)
            log.info(
                "%s indexed %s chunks (backfill=%s)",
                connector.name,
                count,
                backfill,
            )
        if result.watermarks:
            for key, value in result.watermarks.items():
                self.checkpoints.set(key, value)
        if result.watermark:
            self.checkpoints.set(connector.name, result.watermark)
        return count

    async def _run(self, *, backfill: bool) -> int:
        if not self.connectors:
            log.warning("no connectors enabled; nothing to ingest")
            return 0
        total = 0
        for connector in self.connectors:
            try:
                total += await self.ingest_connector(connector, backfill=backfill)
            except Exception:
                log.exception("%s ingest failed; continuing with other connectors", connector.name)
        return total
