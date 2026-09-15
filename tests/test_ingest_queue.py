from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.testclient import TestClient

from ragtone.board_http import create_app
from ragtone.checkpoints import CheckpointStore
from ragtone.chunking import jira_chunks
from ragtone.embeddings import HashEmbedder
from ragtone.ingest.base import FetchResult, Page, WorkRecord
from ragtone.ingest.jobs import IngestItem, contiguous_watermarks, progress_percent
from ragtone.ingest.queue import JobQueue, MemoryBackend
from ragtone.ingest.worker import IngestWorker
from ragtone.memory_index import InMemoryIndex
from ragtone.settings import FoundationMcp, Settings


def test_contiguous_watermarks_stop_at_dlq() -> None:
    items = [
        IngestItem(id="a", run_id="r", connector="jira", ref="A", status="ok", seq=0, watermark="1"),
        IngestItem(id="b", run_id="r", connector="jira", ref="B", status="dlq", seq=1, watermark="2"),
        IngestItem(id="c", run_id="r", connector="jira", ref="C", status="ok", seq=2, watermark="3"),
    ]
    assert contiguous_watermarks(items) == {"jira": "1"}


def test_progress_percent_uses_jira_total() -> None:
    from ragtone.ingest.jobs import IngestRun

    run = IngestRun(
        id="r",
        connector="jira",
        total=10,
        discovered=4,
        indexed=2,
        dlq=1,
        producer_done=False,
    )
    assert progress_percent(run) == 30


def test_queue_indexes_items_and_saves_contiguous_watermark(tmp_path: Path) -> None:
    queue = JobQueue()
    run = queue.create_run("jira", backfill=True)
    queue.accept_page(
        run.id,
        Page(
            records=[
                WorkRecord(ref="ABC-1", payload={}, watermark="2026-01-01"),
                WorkRecord(ref="ABC-2", payload={}, watermark="2026-02-01"),
            ],
            total=2,
            done=True,
        ),
    )
    first = queue.claim_item(["jira"])
    assert first is not None and first.ref == "ABC-1"
    queue.succeed(first, chunks=1, watermark="2026-01-01")
    second = queue.claim_item(["jira"])
    assert second is not None
    queue.fail_item(second, "boom", max_attempts=1)
    checkpoints = CheckpointStore(tmp_path / "checkpoints.json")
    assert queue.try_finish(run.id, checkpoints) is True
    assert checkpoints.get("jira") == "2026-01-01"
    finished = queue.get_run(run.id)
    assert finished is not None
    assert finished.status == "ok"
    assert finished.dlq == 1
    assert finished.indexed == 1
    job = queue.admin_job()
    assert job["percent"] == 100
    assert job["dead_letters"][0]["ref"] == "ABC-2"


def test_queue_retries_until_dlq_then_manual_retry() -> None:
    queue = JobQueue()
    run = queue.create_run("jira", backfill=False)
    queue.accept_page(
        run.id,
        Page(records=[WorkRecord(ref="ABC-9", payload={})], total=1, done=True),
    )
    item = queue.claim_item(["jira"])
    assert item is not None
    queue.fail_item(item, "once", max_attempts=2)
    assert queue.get_item(item.id).status == "retry"
    again = queue.claim_item(["jira"])
    assert again is not None
    queue.fail_item(again, "twice", max_attempts=2)
    dead = queue.get_item(item.id)
    assert dead is not None
    assert dead.status == "dlq"
    queue.retry_item(item.id)
    revived = queue.get_item(item.id)
    assert revived is not None
    assert revived.status == "pending"
    assert queue.get_run(run.id).status == "running"
    assert queue.get_run(run.id).dlq == 0


def test_lease_is_exclusive() -> None:
    queue = JobQueue()
    assert queue.try_lease("sync") is True
    assert queue.try_lease("board") is False
    queue.release_lease("sync")
    assert queue.try_lease("board") is True


def test_reacquiring_own_lease_does_not_write_again() -> None:
    class RecordingBackend(MemoryBackend):
        def __init__(self) -> None:
            super().__init__()
            self.lease_writes = 0

        def save_lease(self, holder: str, until: str) -> None:
            self.lease_writes += 1
            super().save_lease(holder, until)

    backend = RecordingBackend()
    queue = JobQueue(backend)
    assert queue.try_lease("sync") is True
    assert queue.try_lease("sync") is True
    assert backend.lease_writes == 1


class _Scripted:
    name = "jira"

    def __init__(self) -> None:
        self.pages = 0

    async def next_page(self, checkpoint, *, backfill, cursor) -> Page:
        self.pages += 1
        if cursor:
            return Page(done=True)
        return Page(
            records=[WorkRecord(ref="ABC-1", payload={}, watermark="2026-03-01")],
            total=1,
            done=True,
        )

    async def materialize(self, record: WorkRecord) -> FetchResult:
        return FetchResult(
            chunks=jira_chunks(key="ABC-1", summary="Login", description="Timeout"),
            watermark="2026-03-01",
        )


def test_worker_drains_queue_pages(tmp_path: Path) -> None:
    store = InMemoryIndex()
    checkpoints = CheckpointStore(tmp_path / "checkpoints.json")
    worker = IngestWorker(
        store,
        HashEmbedder(8),
        checkpoints,
        [_Scripted()],
        poll_seconds=1,
        queue=JobQueue(),
    )
    count = asyncio.run(worker.backfill())
    assert count == 1
    assert checkpoints.get("jira") == "2026-03-01"
    assert store.by_issue("ABC-1")


def test_queue_stops_a_run_when_a_page_repeats_only_known_records() -> None:
    queue = JobQueue()
    run = queue.create_run("jira", backfill=True)
    first = Page(
        records=[WorkRecord(ref="ABC-1", payload={})],
        cursor={"nextPageToken": "again"},
        done=False,
    )
    queue.accept_page(run.id, first)
    repeated = queue.accept_page(run.id, first)
    assert repeated.discovered == 1
    assert repeated.producer_done is True
    assert repeated.error == "pagination repeated an already discovered page"


def test_board_http_enqueues_sync_on_injected_queue(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        embedder="hash",
        jira={"enabled": True, "mcp": "atlassian", "projects": ["ABC"]},
        foundation_mcps=[FoundationMcp(name="atlassian", url="http://127.0.0.1:3001/mcp")],
    )
    app = create_app(settings)
    app.state.ctx._queue = JobQueue()
    client = TestClient(app)
    response = client.post("/api/admin/sync", json={"name": "jira", "backfill": True})
    assert response.status_code == 200
    job = response.json()["job"]
    assert job["status"] == "running"
    assert job["backfill"] is True
    assert job["connector"] == "jira"
    again = client.post("/api/admin/sync", json={"name": "jira"})
    assert again.status_code == 409


def test_board_http_admin_shows_queue_progress(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    app = create_app(settings)
    queue = JobQueue()
    run = queue.create_run("jira", backfill=True)
    queue.accept_page(
        run.id,
        Page(records=[WorkRecord(ref="ABC-1", payload={})], total=4, done=False),
    )
    app.state.ctx._queue = queue
    data = TestClient(app).get("/api/admin").json()
    assert data["job"]["status"] == "running"
    assert data["job"]["discovered"] == 1
    assert data["job"]["total"] == 4
    assert data["job"]["percent"] == 0


def test_board_http_retries_dlq(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, embedder="hash")
    app = create_app(settings)
    queue = JobQueue()
    run = queue.create_run("jira", backfill=False)
    queue.accept_page(
        run.id,
        Page(records=[WorkRecord(ref="ABC-1", payload={})], total=1, done=True),
    )
    item = queue.claim_item(["jira"])
    assert item is not None
    queue.fail_item(item, "boom", max_attempts=1)
    app.state.ctx._queue = queue
    response = TestClient(app).post("/api/admin/dlq/retry", json={"id": item.id})
    assert response.status_code == 200
    assert queue.get_item(item.id).status == "pending"
    assert response.json()["job"]["dlq"] == 0
