from __future__ import annotations

import asyncio
from ragtone.chunking import jira_chunks
from ragtone.ingest.base import FetchResult
from ragtone.ingest.client import ToolCaller
from ragtone.ingest.parse import as_records, as_text, iso_days_ago, later_watermark
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
    ) -> None:
        self.source = source
        self.caller = caller
        self.cloud_id = cloud_id
        self.backfill_days = backfill_days
        self.pause = pause

    async def fetch(self, checkpoint: str | None, *, backfill: bool) -> FetchResult:
        if not self.source.projects:
            return FetchResult()
        stamp = checkpoint if checkpoint and not backfill else iso_days_ago(self.backfill_days)
        jql = scoped_jql(self.source.projects, self.source.jql, stamp)
        payload = await self.caller.call_tool(
            self.source.search_tool,
            {"jql": jql, "cloudId": self.cloud_id, "maxResults": 50},
        )
        issues = as_records(payload, "issues", "results", "values")
        chunks = []
        newest = checkpoint
        for issue in issues:
            key = str(issue.get("key") or issue.get("id") or "")
            if not key:
                continue
            detail = issue
            if self.source.get_tool and "fields" not in issue:
                fetched = await self.caller.call_tool(
                    self.source.get_tool,
                    {"issueIdOrKey": key, "cloudId": self.cloud_id},
                )
                if isinstance(fetched, dict):
                    detail = fetched
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
            updated = str(fields.get("updated") or detail.get("updated") or "")
            chunks.extend(
                jira_chunks(
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
            )
            newest = later_watermark(newest, updated)
            await asyncio.sleep(self.pause)
        return FetchResult(chunks=chunks, watermark=newest or stamp)


def build_jira(settings: Settings, callers: dict[str, ToolCaller]) -> JiraConnector | None:
    if not settings.jira.enabled:
        return None
    return JiraConnector(
        settings.jira,
        callers[settings.jira.mcp],
        cloud_id=settings.atlassian_cloud_id,
        backfill_days=settings.backfill_days,
        pause=settings.mcp_pause_seconds,
    )
