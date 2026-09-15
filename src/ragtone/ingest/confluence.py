from __future__ import annotations

import asyncio
from typing import Any

from ragtone.chunking import confluence_chunks
from ragtone.ingest.base import FetchResult, Page, WorkRecord, fetch_all
from ragtone.ingest.client import ToolCaller
from ragtone.ingest.page import confluence_next, search_page
from ragtone.ingest.parse import as_text, iso_days_ago, later_watermark
from ragtone.settings import ConfluenceSource, Settings


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
    ) -> None:
        self.source = source
        self.caller = caller
        self.cloud_id = cloud_id
        self.backfill_days = backfill_days
        self.pause = pause

    async def fetch(self, checkpoint: str | None, *, backfill: bool) -> FetchResult:
        return await fetch_all(self, checkpoint, backfill=backfill)

    async def next_page(
        self,
        checkpoint: str | None,
        *,
        backfill: bool,
        cursor: dict[str, Any] | None,
    ) -> Page:
        if not self.source.docs:
            return Page(done=True)
        stamp = checkpoint if checkpoint and not backfill else iso_days_ago(self.backfill_days)
        cql = scoped_cql(self.source.docs, self.source.cql, stamp)
        page = await search_page(
            self.caller,
            self.source.search_tool,
            {"cql": cql, "cloudId": self.cloud_id, "limit": 25},
            "results",
            "pages",
            "values",
            next_args=confluence_next,
            extra=cursor,
        )
        if self.pause:
            await asyncio.sleep(self.pause)
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
                    checkpoint_key="confluence",
                )
            )
        return Page(
            records=records,
            cursor=page.next_args,
            total=page.total,
            done=not page.next_args,
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
        chunks = confluence_chunks(
            page_id=page_id,
            title=title,
            body=body or title,
            url=str(detail.get("url") or detail.get("_links", {}).get("webui") or ""),
            space=str(detail.get("space") or detail.get("spaceKey") or ""),
            updated_at=updated or None,
        )
        newest = later_watermark(record.watermark, updated)
        if self.pause:
            await asyncio.sleep(self.pause)
        return FetchResult(
            chunks=chunks,
            watermark=newest,
            watermarks={"confluence": newest} if newest else {},
        )


def build_confluence(
    settings: Settings, callers: dict[str, ToolCaller]
) -> ConfluenceConnector | None:
    if not settings.confluence.enabled:
        return None
    return ConfluenceConnector(
        settings.confluence,
        callers[settings.confluence.mcp],
        cloud_id=settings.atlassian_cloud_id,
        backfill_days=settings.backfill_days,
        pause=settings.mcp_pause_seconds,
    )
