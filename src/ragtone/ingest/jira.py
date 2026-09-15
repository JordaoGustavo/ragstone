from __future__ import annotations

import asyncio
from typing import Any

from ragtone.checkpoints import CheckpointStore
from ragtone.chunking import jira_chunks
from ragtone.ingest.base import FetchResult, Page, WorkRecord, fetch_all
from ragtone.ingest.client import ToolCaller
from ragtone.ingest.page import jira_next, search_page
from ragtone.ingest.parse import as_records, as_text, later_watermark, window_stamp
from ragtone.settings import JiraSource, Settings


def scoped_jql(projects: list[str], template: str, stamp: str) -> str:
    timed = template.format(checkpoint=stamp)
    order = ""
    marker = " ORDER BY "
    idx = timed.upper().rfind(marker)
    if idx >= 0:
        order = timed[idx:]
        timed = timed[:idx]
    if projects:
        joined = ", ".join(projects)
        timed = f"project in ({joined}) AND ({timed})"
    return timed + order


class JiraConnector:
    name = "jira"

    def __init__(
        self,
        source: JiraSource,
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
        project: str,
        checkpoint: str | None,
        *,
        backfill: bool,
        backfill_days: int | None,
    ) -> str:
        keyed = self.checkpoints.get(f"jira:{project}") if self.checkpoints is not None else None
        return window_stamp(
            keyed=keyed,
            legacy=checkpoint,
            cutoff=self.source.project_cutoffs.get(project),
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
    ) -> Page:
        projects = self.source.projects
        if not projects:
            return Page(done=True)
        state = dict(cursor or {"i": 0})
        index = int(state.get("i") or 0)
        if index >= len(projects):
            return Page(done=True)
        project = projects[index]
        stamp = self._stamp(project, checkpoint, backfill=backfill, backfill_days=backfill_days)
        extra = state.get("s") if isinstance(state.get("s"), dict) else None
        jql = scoped_jql([project], self.source.jql, stamp)
        page = await search_page(
            self.caller,
            self.source.search_tool,
            {"jql": jql, "cloudId": self.cloud_id, "maxResults": 50},
            "issues",
            "results",
            "values",
            next_args=jira_next,
            extra=extra,
        )
        if self.pause:
            await asyncio.sleep(self.pause)
        key = f"jira:{project}"
        records: list[WorkRecord] = []
        for issue in page.records:
            issue_key = str(issue.get("key") or issue.get("id") or "")
            if not issue_key:
                continue
            fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else issue
            updated = str(fields.get("updated") or issue.get("updated") or "")
            records.append(
                WorkRecord(
                    ref=issue_key,
                    payload=issue,
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
        done = next_index >= len(projects)
        return Page(
            records=records,
            cursor=None if done else {"i": next_index},
            total=page.total,
            done=done,
        )

    async def materialize(self, record: WorkRecord) -> FetchResult:
        issue = record.payload
        key = record.ref
        detail = issue
        if self.source.get_tool and "fields" not in issue:
            fetched = await self.caller.call_tool(
                self.source.get_tool,
                {"issueIdOrKey": key, "cloudId": self.cloud_id},
            )
            if isinstance(fetched, dict):
                detail = fetched
            if self.pause:
                await asyncio.sleep(self.pause)
        fields = detail.get("fields") if isinstance(detail.get("fields"), dict) else detail
        comments = []
        raw_comments = fields.get("comment") if isinstance(fields, dict) else None
        comment_list = []
        if isinstance(raw_comments, dict):
            comment_list = as_records(raw_comments, "comments")
        elif isinstance(raw_comments, list):
            comment_list = [item for item in raw_comments if isinstance(item, dict)]
        for comment in comment_list:
            comments.append(
                {
                    "id": str(comment.get("id") or comment.get("created") or len(comments)),
                    "body": as_text(comment.get("body")),
                    "author": as_text(
                        (comment.get("author") or {}).get("displayName")
                        if isinstance(comment.get("author"), dict)
                        else comment.get("author")
                    ),
                    "updated_at": str(comment.get("updated") or comment.get("created") or ""),
                }
            )
        updated = str(fields.get("updated") or detail.get("updated") or record.watermark or "")
        chunks = jira_chunks(
            key=key,
            summary=as_text(fields.get("summary") or detail.get("summary")),
            description=as_text(fields.get("description") or detail.get("description")),
            url=str(detail.get("self") or detail.get("url") or ""),
            comments=comments,
            updated_at=updated or None,
            authors=tuple(
                filter(
                    None,
                    [
                        as_text(
                            (fields.get("assignee") or {}).get("displayName")
                            if isinstance(fields.get("assignee"), dict)
                            else None
                        )
                    ],
                )
            ),
        )
        newest = later_watermark(record.watermark, updated)
        if self.pause:
            await asyncio.sleep(self.pause)
        mark_key = record.checkpoint_key or "jira"
        return FetchResult(
            chunks=chunks,
            watermark=newest,
            watermarks={mark_key: newest} if newest else {},
        )


def build_jira(
    settings: Settings,
    callers: dict[str, ToolCaller],
    checkpoints: CheckpointStore | None = None,
) -> JiraConnector | None:
    if not settings.jira.enabled:
        return None
    return JiraConnector(
        settings.jira,
        callers[settings.jira.mcp],
        cloud_id=settings.atlassian_cloud_id,
        backfill_days=settings.backfill_days,
        pause=settings.mcp_pause_seconds,
        checkpoints=checkpoints,
    )
