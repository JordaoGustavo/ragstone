from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ragtone.ingest.parse import later_watermark

UNFINISHED = ("pending", "retry", "running")
ACTIVE = ("pending", "running")
CANCELLED = "cancelled"
MAX_ATTEMPTS = 3
CLAIM_STALE = timedelta(seconds=120)
LEASE_TTL = timedelta(seconds=60)
LEASE_ID = "_lease"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def later_iso(delta: timedelta, *, start: str | None = None) -> str:
    if start:
        current = datetime.fromisoformat(start.replace("Z", "+00:00"))
    else:
        current = datetime.now(timezone.utc)
    return (current + delta).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class IngestRun:
    id: str
    connector: str
    backfill: bool = False
    backfill_days: int | None = None
    targets: list[str] | None = None
    status: str = "pending"
    producer_done: bool = False
    page_cursor: dict[str, Any] | None = None
    total: int | None = None
    discovered: int = 0
    indexed: int = 0
    dlq: int = 0
    chunks: int = 0
    pages: int = 0
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    seq_next: int = 0

    def to_doc(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["doc_type"] = "run"
        return doc


@dataclass
class IngestItem:
    id: str
    run_id: str
    connector: str
    ref: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    seq: int = 0
    attempts: int = 0
    error: str | None = None
    watermark: str | None = None
    checkpoint_key: str | None = None
    claimed_at: str | None = None
    next_retry_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    seq_no: int | None = field(default=None, compare=False, repr=False)
    primary_term: int | None = field(default=None, compare=False, repr=False)

    def to_doc(self) -> dict[str, Any]:
        doc = asdict(self)
        doc.pop("seq_no", None)
        doc.pop("primary_term", None)
        return doc


def run_from_doc(doc: dict[str, Any], *, run_id: str | None = None) -> IngestRun:
    return IngestRun(
        id=str(doc.get("id") or run_id or ""),
        connector=str(doc.get("connector") or ""),
        backfill=bool(doc.get("backfill")),
        backfill_days=int(doc["backfill_days"]) if doc.get("backfill_days") is not None else None,
        targets=_targets_from_doc(doc.get("targets")),
        status=str(doc.get("status") or "pending"),
        producer_done=bool(doc.get("producer_done")),
        page_cursor=doc.get("page_cursor") if isinstance(doc.get("page_cursor"), dict) else None,
        total=int(doc["total"]) if doc.get("total") is not None else None,
        discovered=int(doc.get("discovered") or 0),
        indexed=int(doc.get("indexed") or 0),
        dlq=int(doc.get("dlq") or 0),
        chunks=int(doc.get("chunks") or 0),
        pages=int(doc.get("pages") or 0),
        error=str(doc["error"]) if doc.get("error") else None,
        created_at=str(doc.get("created_at") or ""),
        updated_at=str(doc.get("updated_at") or ""),
        seq_next=int(doc.get("seq_next") or 0),
    )


def _targets_from_doc(raw: object) -> list[str] | None:
    if not isinstance(raw, list):
        return None
    cleaned = [str(item) for item in raw if str(item).strip()]
    return cleaned or None


def item_from_doc(doc: dict[str, Any], *, item_id: str | None = None) -> IngestItem:
    payload = doc.get("payload")
    return IngestItem(
        id=str(doc.get("id") or item_id or ""),
        run_id=str(doc.get("run_id") or ""),
        connector=str(doc.get("connector") or ""),
        ref=str(doc.get("ref") or ""),
        payload=payload if isinstance(payload, dict) else {},
        status=str(doc.get("status") or "pending"),
        seq=int(doc.get("seq") or 0),
        attempts=int(doc.get("attempts") or 0),
        error=str(doc["error"]) if doc.get("error") else None,
        watermark=str(doc["watermark"]) if doc.get("watermark") else None,
        checkpoint_key=str(doc["checkpoint_key"]) if doc.get("checkpoint_key") else None,
        claimed_at=str(doc["claimed_at"]) if doc.get("claimed_at") else None,
        next_retry_at=str(doc["next_retry_at"]) if doc.get("next_retry_at") else None,
        created_at=str(doc.get("created_at") or ""),
        updated_at=str(doc.get("updated_at") or ""),
    )


def contiguous_watermarks(items: Iterable[IngestItem]) -> dict[str, str]:
    grouped: dict[str, list[IngestItem]] = {}
    for item in items:
        key = item.checkpoint_key or item.connector
        grouped.setdefault(key, []).append(item)
    out: dict[str, str] = {}
    for key, group in grouped.items():
        group.sort(key=lambda item: item.seq)
        latest: str | None = None
        for item in group:
            if item.status != "ok":
                break
            if item.watermark:
                latest = later_watermark(latest, item.watermark)
        if latest:
            out[key] = latest
    return out


def progress_percent(run: IngestRun) -> int | None:
    processed = run.indexed + run.dlq
    denom = run.total
    if denom is None and run.producer_done:
        denom = run.discovered
    if denom is None:
        return None
    if denom <= 0:
        return 100 if run.producer_done else 0
    return min(100, int(100 * processed / denom))


def letter_view(item: IngestItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "run_id": item.run_id,
        "connector": item.connector,
        "ref": item.ref,
        "error": item.error,
        "attempts": item.attempts,
    }


def job_view(
    run: IngestRun | None,
    *,
    dead_letters: list[IngestItem] | None = None,
    runs: list[IngestRun] | None = None,
) -> dict[str, Any]:
    letters = [letter_view(item) for item in dead_letters or []]
    listed = runs or ([run] if run is not None else [])
    if run is None:
        return {
            "status": "idle",
            "connector": None,
            "backfill": False,
            "backfill_days": None,
            "targets": None,
            "chunks": 0,
            "error": None,
            "percent": None,
            "discovered": 0,
            "indexed": 0,
            "dlq": 0,
            "total": None,
            "producer_done": False,
            "dead_letters": letters,
            "runs": [],
        }
    status = "running" if run.status in ACTIVE else run.status
    if len(listed) > 1 and (
        any(item.status in ACTIVE for item in listed)
        or len({item.status for item in listed}) == 1
    ):
        if any(item.status in ACTIVE for item in listed):
            status = "running"
        connector = "all" if len({item.connector for item in listed}) > 1 else run.connector
        chunks = sum(item.chunks for item in listed)
        discovered = sum(item.discovered for item in listed)
        indexed = sum(item.indexed for item in listed)
        dlq = sum(item.dlq for item in listed)
        totals = [item.total for item in listed if item.total is not None]
        total = sum(totals) if len(totals) == len(listed) else None
        backfill = any(item.backfill for item in listed)
        backfill_days = next((item.backfill_days for item in listed if item.backfill), None)
        targets = next((list(item.targets) for item in listed if item.targets), None)
        percent = progress_percent(
            IngestRun(
                id="",
                connector=connector,
                producer_done=all(item.producer_done for item in listed),
                total=total,
                discovered=discovered,
                indexed=indexed,
                dlq=dlq,
            )
        )
        error = next((item.error for item in listed if item.error), None)
        return {
            "status": status,
            "connector": connector,
            "backfill": backfill,
            "backfill_days": backfill_days,
            "targets": targets,
            "chunks": chunks,
            "error": error,
            "percent": percent,
            "discovered": discovered,
            "indexed": indexed,
            "dlq": dlq,
            "total": total,
            "producer_done": all(item.producer_done for item in listed),
            "dead_letters": letters,
            "runs": [_run_summary(item) for item in listed],
        }
    return {
        "status": status,
        "connector": run.connector,
        "backfill": run.backfill,
        "backfill_days": run.backfill_days if run.backfill else None,
        "targets": list(run.targets) if run.targets else None,
        "chunks": run.chunks,
        "error": run.error,
        "percent": progress_percent(run),
        "discovered": run.discovered,
        "indexed": run.indexed,
        "dlq": run.dlq,
        "total": run.total,
        "producer_done": run.producer_done,
        "dead_letters": letters,
        "runs": [_run_summary(item) for item in listed],
    }


def _run_summary(run: IngestRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "connector": run.connector,
        "status": run.status,
        "backfill": run.backfill,
        "backfill_days": run.backfill_days if run.backfill else None,
        "targets": list(run.targets) if run.targets else None,
        "percent": progress_percent(run),
        "discovered": run.discovered,
        "indexed": run.indexed,
        "dlq": run.dlq,
        "chunks": run.chunks,
        "total": run.total,
    }
