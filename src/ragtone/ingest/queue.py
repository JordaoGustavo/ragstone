from __future__ import annotations

import uuid
from typing import Any, Iterable, Sequence

from ragtone.checkpoints import CheckpointStore
from ragtone.ingest.base import Page, WorkRecord
from ragtone.ingest.jobs import (
    ACTIVE,
    CLAIM_STALE,
    LEASE_ID,
    LEASE_TTL,
    MAX_ATTEMPTS,
    UNFINISHED,
    IngestItem,
    IngestRun,
    contiguous_watermarks,
    item_from_doc,
    job_view,
    later_iso,
    now_iso,
    run_from_doc,
)


class MemoryBackend:
    def __init__(self) -> None:
        self.runs: dict[str, IngestRun] = {}
        self.items: dict[str, IngestItem] = {}
        self.lease: tuple[str, str] | None = None

    def ensure(self) -> None:
        return None

    def save_run(self, run: IngestRun) -> None:
        self.runs[run.id] = run

    def load_run(self, run_id: str) -> IngestRun | None:
        return self.runs.get(run_id)

    def save_item(self, item: IngestItem, *, seq_no: int | None = None, primary_term: int | None = None) -> None:
        self.items[item.id] = item

    def load_item(self, item_id: str) -> IngestItem | None:
        return self.items.get(item_id)

    def find_claimable(
        self,
        *,
        connectors: Sequence[str] | None,
        now: str,
        stale_before: str,
    ) -> IngestItem | None:
        found: list[IngestItem] = []
        for item in self.items.values():
            if connectors is not None and item.connector not in connectors:
                continue
            if item.status == "pending":
                found.append(item)
            elif item.status == "retry" and (item.next_retry_at or "") <= now:
                found.append(item)
            elif item.status == "running" and (item.claimed_at or "") <= stale_before:
                found.append(item)
        found.sort(key=lambda item: item.seq)
        return found[0] if found else None

    def iter_items(self, run_id: str) -> Iterable[IngestItem]:
        items = [item for item in self.items.values() if item.run_id == run_id]
        items.sort(key=lambda item: item.seq)
        return items

    def unfinished_count(self, run_id: str) -> int:
        return sum(
            1
            for item in self.items.values()
            if item.run_id == run_id and item.status in UNFINISHED
        )

    def list_runs(
        self,
        *,
        statuses: Sequence[str] | None = None,
        connector: str | None = None,
        limit: int = 50,
    ) -> list[IngestRun]:
        runs = list(self.runs.values())
        if statuses is not None:
            runs = [run for run in runs if run.status in statuses]
        if connector is not None:
            runs = [run for run in runs if run.connector == connector]
        runs.sort(key=lambda run: run.created_at, reverse=True)
        return runs[:limit]

    def list_dlq(self, *, limit: int = 50) -> list[IngestItem]:
        items = [item for item in self.items.values() if item.status == "dlq"]
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return items[:limit]

    def load_lease(self) -> tuple[str, str] | None:
        return self.lease

    def save_lease(self, holder: str, until: str) -> None:
        self.lease = (holder, until)

    def delete_lease(self) -> None:
        self.lease = None


class ElasticsearchBackend:
    def __init__(self, es: Any, chunks_index: str) -> None:
        self.es = es
        self.runs_index = f"{chunks_index}_ingest_runs"
        self.items_index = f"{chunks_index}_ingest_items"

    def ensure(self) -> None:
        if not self.es.indices.exists(index=self.runs_index):
            self.es.indices.create(index=self.runs_index, mappings=_run_mappings())
        if not self.es.indices.exists(index=self.items_index):
            self.es.indices.create(index=self.items_index, mappings=_item_mappings())

    def save_run(self, run: IngestRun) -> None:
        self.es.index(index=self.runs_index, id=run.id, document=run.to_doc(), refresh=True)

    def load_run(self, run_id: str) -> IngestRun | None:
        from elasticsearch import NotFoundError

        try:
            hit = self.es.get(index=self.runs_index, id=run_id)
        except NotFoundError:
            return None
        return run_from_doc(hit.get("_source") or {}, run_id=run_id)

    def save_item(
        self,
        item: IngestItem,
        *,
        seq_no: int | None = None,
        primary_term: int | None = None,
    ) -> None:
        doc = item.to_doc()
        if seq_no is not None and primary_term is not None:
            self.es.update(
                index=self.items_index,
                id=item.id,
                doc=doc,
                if_seq_no=seq_no,
                if_primary_term=primary_term,
                refresh=True,
            )
            return
        self.es.index(index=self.items_index, id=item.id, document=doc, refresh=True)

    def load_item(self, item_id: str) -> IngestItem | None:
        from elasticsearch import NotFoundError

        try:
            hit = self.es.get(
                index=self.items_index,
                id=item_id,
                seq_no_primary_term=True,
            )
        except NotFoundError:
            return None
        item = item_from_doc(hit.get("_source") or {}, item_id=item_id)
        item.seq_no = hit.get("_seq_no")
        item.primary_term = hit.get("_primary_term")
        return item

    def find_claimable(
        self,
        *,
        connectors: Sequence[str] | None,
        now: str,
        stale_before: str,
    ) -> IngestItem | None:
        filters: list[dict[str, Any]] = []
        if connectors is not None:
            filters.append({"terms": {"connector": list(connectors)}})
        query: dict[str, Any] = {
            "bool": {
                "filter": filters,
                "should": [
                    {"term": {"status": "pending"}},
                    {
                        "bool": {
                            "must": [
                                {"term": {"status": "retry"}},
                                {"range": {"next_retry_at": {"lte": now}}},
                            ]
                        }
                    },
                    {
                        "bool": {
                            "must": [
                                {"term": {"status": "running"}},
                                {"range": {"claimed_at": {"lte": stale_before}}},
                            ]
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        }
        response = self.es.search(
            index=self.items_index,
            query=query,
            sort=[{"seq": "asc"}],
            size=1,
            seq_no_primary_term=True,
        )
        hits = response.get("hits", {}).get("hits") or []
        if not hits:
            return None
        hit = hits[0]
        item = item_from_doc(hit.get("_source") or {}, item_id=str(hit["_id"]))
        item.seq_no = hit.get("_seq_no")
        item.primary_term = hit.get("_primary_term")
        return item

    def iter_items(self, run_id: str) -> Iterable[IngestItem]:
        search_after = None
        while True:
            kwargs: dict[str, Any] = {
                "index": self.items_index,
                "query": {"term": {"run_id": run_id}},
                "sort": [{"seq": "asc"}, {"id": "asc"}],
                "size": 500,
                "source_includes": [
                    "id",
                    "run_id",
                    "connector",
                    "ref",
                    "status",
                    "seq",
                    "watermark",
                    "checkpoint_key",
                    "attempts",
                    "error",
                ],
            }
            if search_after is not None:
                kwargs["search_after"] = search_after
            response = self.es.search(**kwargs)
            hits = response.get("hits", {}).get("hits") or []
            if not hits:
                return
            for hit in hits:
                yield item_from_doc(hit.get("_source") or {}, item_id=str(hit["_id"]))
            search_after = hits[-1]["sort"]

    def unfinished_count(self, run_id: str) -> int:
        response = self.es.count(
            index=self.items_index,
            query={
                "bool": {
                    "filter": [
                        {"term": {"run_id": run_id}},
                        {"terms": {"status": list(UNFINISHED)}},
                    ]
                }
            },
        )
        return int(response.get("count") or 0)

    def list_runs(
        self,
        *,
        statuses: Sequence[str] | None = None,
        connector: str | None = None,
        limit: int = 50,
    ) -> list[IngestRun]:
        filters: list[dict[str, Any]] = [{"term": {"doc_type": "run"}}]
        if statuses is not None:
            filters.append({"terms": {"status": list(statuses)}})
        if connector is not None:
            filters.append({"term": {"connector": connector}})
        response = self.es.search(
            index=self.runs_index,
            query={"bool": {"filter": filters}},
            sort=[{"created_at": {"order": "desc"}}],
            size=limit,
        )
        runs: list[IngestRun] = []
        for hit in response.get("hits", {}).get("hits") or []:
            runs.append(run_from_doc(hit.get("_source") or {}, run_id=str(hit["_id"])))
        return runs

    def list_dlq(self, *, limit: int = 50) -> list[IngestItem]:
        response = self.es.search(
            index=self.items_index,
            query={"term": {"status": "dlq"}},
            sort=[{"updated_at": {"order": "desc"}}],
            size=limit,
        )
        return [
            item_from_doc(hit.get("_source") or {}, item_id=str(hit["_id"]))
            for hit in response.get("hits", {}).get("hits") or []
        ]

    def load_lease(self) -> tuple[str, str] | None:
        from elasticsearch import NotFoundError

        try:
            hit = self.es.get(index=self.runs_index, id=LEASE_ID)
        except NotFoundError:
            return None
        src = hit.get("_source") or {}
        holder = str(src.get("holder") or "")
        until = str(src.get("until") or "")
        if not holder or not until:
            return None
        return holder, until

    def save_lease(self, holder: str, until: str) -> None:
        self.es.index(
            index=self.runs_index,
            id=LEASE_ID,
            document={"doc_type": "lease", "holder": holder, "until": until},
            refresh=True,
        )

    def delete_lease(self) -> None:
        from elasticsearch import NotFoundError

        try:
            self.es.delete(index=self.runs_index, id=LEASE_ID, refresh=True)
        except NotFoundError:
            return


class JobQueue:
    def __init__(self, backend: MemoryBackend | ElasticsearchBackend | None = None) -> None:
        self._b = backend or MemoryBackend()

    @classmethod
    def elasticsearch(cls, es: Any, chunks_index: str) -> JobQueue:
        return cls(ElasticsearchBackend(es, chunks_index))

    def ensure(self) -> None:
        self._b.ensure()

    def create_run(
        self, connector: str, *, backfill: bool, backfill_days: int | None = None
    ) -> IngestRun:
        stamp = now_iso()
        run = IngestRun(
            id=uuid.uuid4().hex,
            connector=connector,
            backfill=backfill,
            backfill_days=backfill_days if backfill else None,
            status="pending",
            created_at=stamp,
            updated_at=stamp,
        )
        self._b.save_run(run)
        return run

    def get_run(self, run_id: str) -> IngestRun | None:
        return self._b.load_run(run_id)

    def get_item(self, item_id: str) -> IngestItem | None:
        return self._b.load_item(item_id)

    def active_runs(self, connector: str | None = None) -> list[IngestRun]:
        return self._b.list_runs(statuses=ACTIVE, connector=connector)

    def active_for(self, connector: str) -> bool:
        return bool(self.active_runs(connector))

    def latest_run(self, connector: str | None = None) -> IngestRun | None:
        runs = self._b.list_runs(connector=connector, limit=1)
        return runs[0] if runs else None

    def accept_page(self, run_id: str, page: Page) -> IngestRun:
        run = self._require_run(run_id)
        stamp = now_iso()
        for record in page.records:
            item = IngestItem(
                id=f"{run.id}:{record.ref}",
                run_id=run.id,
                connector=run.connector,
                ref=record.ref,
                payload=record.payload,
                status="pending",
                seq=run.seq_next,
                watermark=record.watermark,
                checkpoint_key=record.checkpoint_key or run.connector,
                created_at=stamp,
                updated_at=stamp,
            )
            self._b.save_item(item)
            run.seq_next += 1
            run.discovered += 1
        run.pages += 1
        if page.total is not None:
            run.total = page.total
        run.page_cursor = page.cursor
        run.producer_done = page.done
        run.status = "running"
        run.updated_at = stamp
        self._b.save_run(run)
        return run

    def next_to_produce(self, connectors: Sequence[str] | None = None) -> IngestRun | None:
        waiting = [
            run
            for run in self.active_runs()
            if not run.producer_done and (connectors is None or run.connector in connectors)
        ]
        waiting.sort(key=lambda run: run.created_at)
        return waiting[0] if waiting else None

    def claim_item(self, connectors: Sequence[str] | None = None) -> IngestItem | None:
        from elasticsearch import ConflictError

        now = now_iso()
        stale_before = later_iso(-CLAIM_STALE, start=now)
        item = self._b.find_claimable(connectors=connectors, now=now, stale_before=stale_before)
        if item is None:
            return None
        item.status = "running"
        item.claimed_at = now
        item.updated_at = now
        try:
            self._b.save_item(item, seq_no=item.seq_no, primary_term=item.primary_term)
        except ConflictError:
            return self.claim_item(connectors)
        return item

    def succeed(self, item: IngestItem, *, chunks: int, watermark: str | None) -> None:
        now = now_iso()
        item.status = "ok"
        item.error = None
        item.claimed_at = None
        if watermark:
            item.watermark = watermark
        item.updated_at = now
        self._b.save_item(item)
        run = self._require_run(item.run_id)
        run.indexed += 1
        run.chunks += chunks
        run.status = "running"
        run.updated_at = now
        self._b.save_run(run)

    def fail_item(
        self,
        item: IngestItem,
        error: str,
        *,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        now = now_iso()
        item.attempts += 1
        item.error = error
        item.claimed_at = None
        item.updated_at = now
        run = self._require_run(item.run_id)
        if item.attempts >= max_attempts:
            item.status = "dlq"
            item.next_retry_at = None
            run.dlq += 1
        else:
            item.status = "retry"
            item.next_retry_at = now
        run.updated_at = now
        run.status = "running"
        self._b.save_item(item)
        self._b.save_run(run)

    def fail_run(self, run_id: str, error: str) -> None:
        run = self._require_run(run_id)
        run.status = "error"
        run.error = error
        run.producer_done = True
        run.updated_at = now_iso()
        self._b.save_run(run)

    def try_finish(self, run_id: str, checkpoints: CheckpointStore) -> bool:
        run = self._require_run(run_id)
        if run.status not in ACTIVE:
            return False
        if not run.producer_done:
            return False
        if self._b.unfinished_count(run_id):
            return False
        marks = contiguous_watermarks(self._b.iter_items(run_id))
        for key, value in marks.items():
            checkpoints.set(key, value)
        if run.connector not in marks:
            keyed = [value for key, value in marks.items() if key.startswith(f"{run.connector}:")]
            if keyed:
                checkpoints.set(run.connector, max(keyed))
        run.status = "ok"
        run.updated_at = now_iso()
        self._b.save_run(run)
        return True

    def retry_item(self, item_id: str) -> IngestItem:
        item = self._b.load_item(item_id)
        if item is None:
            raise KeyError(item_id)
        if item.status != "dlq":
            return item
        now = now_iso()
        item.status = "pending"
        item.next_retry_at = None
        item.attempts = 0
        item.updated_at = now
        self._b.save_item(item)
        run = self._require_run(item.run_id)
        run.dlq = max(0, run.dlq - 1)
        run.status = "running"
        run.error = None
        run.updated_at = now
        self._b.save_run(run)
        return item

    def retry_all_dlq(self) -> int:
        count = 0
        for item in list(self._b.list_dlq(limit=500)):
            self.retry_item(item.id)
            count += 1
        return count

    def list_dlq(self, *, limit: int = 50) -> list[IngestItem]:
        return self._b.list_dlq(limit=limit)

    def admin_job(self) -> dict[str, Any]:
        active = self.active_runs()
        dead = self.list_dlq()
        if active:
            newest = active[0]
            return job_view(newest, dead_letters=dead, runs=active)
        latest = self.latest_run()
        if latest is None:
            return job_view(None, dead_letters=dead)
        return job_view(latest, dead_letters=dead, runs=[latest])

    def try_lease(self, holder: str) -> bool:
        now = now_iso()
        until = later_iso(LEASE_TTL, start=now)
        current = self._b.load_lease()
        if current is None or current[1] <= now or current[0] == holder:
            self._b.save_lease(holder, until)
            return True
        return False

    def renew_lease(self, holder: str) -> bool:
        current = self._b.load_lease()
        if current is None or current[0] != holder:
            return False
        self._b.save_lease(holder, later_iso(LEASE_TTL))
        return True

    def release_lease(self, holder: str) -> None:
        current = self._b.load_lease()
        if current is None or current[0] != holder:
            return
        self._b.delete_lease()

    def _require_run(self, run_id: str) -> IngestRun:
        run = self._b.load_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return run


def _run_mappings() -> dict[str, Any]:
    return {
        "properties": {
            "doc_type": {"type": "keyword"},
            "id": {"type": "keyword"},
            "connector": {"type": "keyword"},
            "backfill": {"type": "boolean"},
            "backfill_days": {"type": "integer"},
            "status": {"type": "keyword"},
            "producer_done": {"type": "boolean"},
            "page_cursor": {"type": "object", "enabled": False},
            "total": {"type": "integer"},
            "discovered": {"type": "integer"},
            "indexed": {"type": "integer"},
            "dlq": {"type": "integer"},
            "chunks": {"type": "integer"},
            "pages": {"type": "integer"},
            "seq_next": {"type": "integer"},
            "error": {"type": "text"},
            "holder": {"type": "keyword"},
            "until": {"type": "date", "ignore_malformed": True},
            "created_at": {"type": "date", "ignore_malformed": True},
            "updated_at": {"type": "date", "ignore_malformed": True},
        }
    }


def _item_mappings() -> dict[str, Any]:
    return {
        "properties": {
            "id": {"type": "keyword"},
            "run_id": {"type": "keyword"},
            "connector": {"type": "keyword"},
            "ref": {"type": "keyword"},
            "payload": {"type": "object", "enabled": False},
            "status": {"type": "keyword"},
            "seq": {"type": "integer"},
            "attempts": {"type": "integer"},
            "error": {"type": "text"},
            "watermark": {"type": "keyword"},
            "checkpoint_key": {"type": "keyword"},
            "claimed_at": {"type": "date", "ignore_malformed": True},
            "next_retry_at": {"type": "date", "ignore_malformed": True},
            "created_at": {"type": "date", "ignore_malformed": True},
            "updated_at": {"type": "date", "ignore_malformed": True},
        }
    }
