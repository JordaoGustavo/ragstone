from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Sequence

from ragtone.checkpoints import CheckpointStore
from ragtone.embeddings import Embedder
from ragtone.ingest.base import Connector, WorkRecord
from ragtone.ingest.chat import build_chat
from ragtone.ingest.client import ToolCaller
from ragtone.ingest.confluence import build_confluence
from ragtone.ingest.jira import build_jira
from ragtone.ingest.jobs import MAX_ATTEMPTS
from ragtone.ingest.queue import JobQueue
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
        lease_poll_seconds: int = 5,
        queue: JobQueue | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.checkpoints = checkpoints
        self.connectors = list(connectors)
        self.poll_seconds = poll_seconds
        self.lease_poll_seconds = lease_poll_seconds
        self.queue = queue or JobQueue()
        self.max_attempts = max_attempts
        self._chunks = 0

    @property
    def _names(self) -> list[str]:
        return [connector.name for connector in self.connectors]

    def _connector(self, name: str) -> Connector:
        found = next((item for item in self.connectors if item.name == name), None)
        if found is None:
            raise KeyError(name)
        return found

    async def backfill(self) -> int:
        return await self.enqueue_and_drain(self._names, backfill=True)

    async def poll_once(self) -> int:
        return await self.enqueue_and_drain(self._names, backfill=False)

    async def poll_named(self, name: str, *, backfill: bool = False) -> int:
        if name not in self._names:
            raise KeyError(name)
        return await self.enqueue_and_drain([name], backfill=backfill)

    async def enqueue_and_drain(self, names: Sequence[str], *, backfill: bool) -> int:
        for name in names:
            if not self.queue.active_for(name):
                self.queue.create_run(name, backfill=backfill)
        return await self.drain()

    async def drain(self) -> int:
        self._chunks = 0
        while await self.drain_once():
            self.queue.renew_lease("sync")
        return self._chunks

    async def drain_once(self) -> bool:
        names = self._names
        item = self.queue.claim_item(names)
        if item is not None:
            await self._consume(item)
            return True
        run = self.queue.next_to_produce(names)
        if run is not None:
            await self._produce(run.id, run.connector, run.backfill, run.page_cursor)
            return True
        finished = False
        for active in self.queue.active_runs():
            if active.connector in names:
                finished = self.queue.try_finish(active.id, self.checkpoints) or finished
        return False

    async def run_loop(self, stop: asyncio.Event | None = None, *, backfill_first: bool = False) -> None:
        if backfill_first:
            for name in self._names:
                if not self.queue.active_for(name):
                    self.queue.create_run(name, backfill=True)
        while True:
            if stop is not None and stop.is_set():
                return
            if self.queue.try_lease("sync"):
                self._maybe_enqueue_polls()
                progressed = await self.drain_once()
                if progressed:
                    self.queue.renew_lease("sync")
                    continue
                # Do not retain or renew a lease while idle. This lets board-triggered
                # syncs proceed and avoids a read/write cycle against Elasticsearch.
                self.queue.release_lease("sync")
            else:
                log.debug("sync lease is held by another worker")
            await asyncio.sleep(self.lease_poll_seconds)

    def _maybe_enqueue_polls(self) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        for connector in self.connectors:
            if self.queue.active_for(connector.name):
                continue
            last = self.queue.latest_run(connector.name)
            if last is not None:
                wait = 0 if last.backfill and last.status == "ok" else self.poll_seconds
                if _age_seconds(last.updated_at, now) < wait:
                    continue
            self.queue.create_run(connector.name, backfill=False)

    async def _produce(
        self,
        run_id: str,
        name: str,
        backfill: bool,
        cursor: dict | None,
    ) -> None:
        try:
            connector = self._connector(name)
            checkpoint = self.checkpoints.get(name)
            page = await connector.next_page(checkpoint, backfill=backfill, cursor=cursor)
            run = self.queue.accept_page(run_id, page)
            log.info(
                "%s search page %s: +%s (discovered %s%s)",
                name,
                run.pages,
                len(page.records),
                run.discovered,
                f"/{run.total}" if run.total is not None else "",
            )
        except Exception as exc:
            log.exception("%s ingest failed; continuing with other connectors", name)
            self.queue.fail_run(run_id, str(exc))

    async def _consume(self, item) -> None:
        try:
            connector = self._connector(item.connector)
            result = await connector.materialize(
                WorkRecord(
                    ref=item.ref,
                    payload=item.payload,
                    watermark=item.watermark,
                    checkpoint_key=item.checkpoint_key,
                )
            )
            count = 0
            if result.chunks:
                vectors = self.embedder.embed([chunk.text for chunk in result.chunks])
                self.store.upsert(result.chunks, vectors)
                count = len(result.chunks)
                self._chunks += count
                log.info(
                    "%s indexed %s chunks for %s",
                    item.connector,
                    count,
                    item.ref,
                )
            self.queue.succeed(item, chunks=count, watermark=result.watermark)
        except Exception as exc:
            log.exception("%s item %s failed", item.connector, item.ref)
            self.queue.fail_item(item, str(exc), max_attempts=self.max_attempts)


def _age_seconds(then: str, now: str) -> float:
    def parse(value: str) -> datetime:
        stamp = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(stamp)
        except ValueError:
            return datetime.now(timezone.utc)

    start = parse(then)
    end = parse(now)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0.0, (end - start).total_seconds())
