from __future__ import annotations

import asyncio
from typing import Any, Sequence

from ragtone.checkpoints import CheckpointStore
from ragtone.chunking import confluence_chunks
from ragtone.ingest.base import FetchResult, Page, WorkRecord, fetch_all
from ragtone.ingest.client import ToolCaller
from ragtone.ingest.page import confluence_next, search_page
from ragtone.ingest.parse import as_text, later_watermark, window_stamp
from ragtone.origin import origin_url
from ragtone.settings import ConfluenceSource, Settings
from ragtone.watches import select_watch_ids


def scoped_cql(docs: list[str], template: str, stamp: str) -> str:
    timed = template.format(checkpoint=stamp)
    spaces = [item for item in docs if not item.isdigit()]
    pages = [item for item in docs if item.isdigit()]
    clauses: list[str] = []
    if spaces:
        clauses.append(f"space in ({', '.join(spaces)})")
    if pages:
        joined = ", ".join(pages)
        clauses.append(f"(id in ({joined}) OR ancestor in ({joined}))")
    if not clauses:
        return timed
    return f"({' OR '.join(clauses)}) AND ({timed})"


class ConfluenceConnector:
    name = "confluence"

    def __init__(
        self,
        source: ConfluenceSource,
        caller: ToolCaller,
        *,
        cloud_id: str,
        backfill_days: int,
        pause: float,
        checkpoints: CheckpointStore | None = None,
    ) -> None:
        self.source = source
        self.caller = caller
        self.cloud_id = cloud_id
        self.backfill_days = backfill_days
        self.pause = pause
        self.checkpoints = checkpoints

    def _stamp(
        self,
        doc: str,
        checkpoint: str | None,
        *,
        backfill: bool,
        backfill_days: int | None,
    ) -> str:
        keyed = self.checkpoints.get(f"confluence:{doc}") if self.checkpoints is not None else None
        return window_stamp(
            keyed=keyed,
            legacy=checkpoint,
            cutoff=self.source.doc_cutoffs.get(doc),
            backfill=backfill,
            backfill_days=backfill_days,
            default_days=self.backfill_days,
        )

    async def fetch(self, checkpoint: str | None, *, backfill: bool) -> FetchResult:
        return await fetch_all(self, checkpoint, backfill=backfill)

    async def next_page(
        self,
        checkpoint: str | None,
        *,
        backfill: bool,
        cursor: dict[str, Any] | None,
        backfill_days: int | None = None,
        targets: Sequence[str] | None = None,
    ) -> Page:
        docs = select_watch_ids(self.source.docs, targets)
        if not docs:
            return Page(done=True)
        state = dict(cursor or {"i": 0})
        index = int(state.get("i") or 0)
        if index >= len(docs):
            return Page(done=True)
        doc = docs[index]
        stamp = self._stamp(doc, checkpoint, backfill=backfill, backfill_days=backfill_days)
        extra = state.get("s") if isinstance(state.get("s"), dict) else None
        cql = scoped_cql([doc], self.source.cql, stamp)
        page = await search_page(
            self.caller,
            self.source.search_tool,
            {"cql": cql, "cloudId": self.cloud_id, "limit": 25},
            "results",
            "pages",
            "values",
            next_args=confluence_next,
            extra=extra,
        )
        if self.pause:
            await asyncio.sleep(self.pause)
        key = f"confluence:{doc}"
        records: list[WorkRecord] = []
        for page_doc in page.records:
            page_id = str(page_doc.get("id") or page_doc.get("contentId") or "")
            if not page_id:
                continue
            updated = str(page_doc.get("lastModified") or page_doc.get("updated") or "")
            records.append(
                WorkRecord(
                    ref=page_id,
                    payload=page_doc,
                    watermark=updated or None,
                    checkpoint_key=key,
                )
            )
        if page.next_args:
            return Page(
                records=records,
                cursor={"i": index, "s": page.next_args},
                total=page.total,
                done=False,
            )
        next_index = index + 1
        done = next_index >= len(docs)
        return Page(
            records=records,
            cursor=None if done else {"i": next_index},
            total=page.total,
            done=done,
        )

    async def materialize(self, record: WorkRecord) -> FetchResult:
        page = record.payload
        page_id = record.ref
        detail = page
        if self.source.get_tool and not (page.get("body") or page.get("content")):
            fetched = await self.caller.call_tool(
                self.source.get_tool,
                {"pageId": page_id, "cloudId": self.cloud_id},
            )
            if isinstance(fetched, dict):
                detail = fetched
            if self.pause:
                await asyncio.sleep(self.pause)
        body = as_text(
            detail.get("body") or detail.get("content") or (detail.get("text"))
        )
        updated = str(detail.get("lastModified") or detail.get("updated") or record.watermark or "")
        title = as_text(detail.get("title") or page.get("title") or page_id)
        space_raw = detail.get("space") or detail.get("spaceKey") or page.get("space")
        if isinstance(space_raw, dict):
            space = str(space_raw.get("key") or space_raw.get("spaceKey") or "")
        else:
            space = str(space_raw or "")
        webui = ""
        links = detail.get("_links")
        if isinstance(links, dict):
            webui = str(links.get("webui") or "")
        chunks = confluence_chunks(
            page_id=page_id,
            title=title,
            body=body or title,
            url=origin_url(
                source="confluence",
                native_id=page_id,
                parent_id=page_id,
                channel_or_space=space,
                url=str(detail.get("url") or webui),
                atlassian=self.cloud_id,
            ),
            space=space,
            updated_at=updated or None,
        )
        newest = later_watermark(record.watermark, updated)
        if self.pause:
            await asyncio.sleep(self.pause)
        mark_key = record.checkpoint_key or "confluence"
        return FetchResult(
            chunks=chunks,
            watermark=newest,
            watermarks={mark_key: newest} if newest else {},
        )


def build_confluence(
    settings: Settings,
    callers: dict[str, ToolCaller],
    checkpoints: CheckpointStore | None = None,
) -> ConfluenceConnector | None:
    if not settings.confluence.enabled:
        return None
    return ConfluenceConnector(
        settings.confluence,
        callers[settings.confluence.mcp],
        cloud_id=settings.atlassian_cloud_id,
        backfill_days=settings.backfill_days,
        pause=settings.mcp_pause_seconds,
        checkpoints=checkpoints,
    )
