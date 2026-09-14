from __future__ import annotations

import asyncio

from ragtone.checkpoints import CheckpointStore
from ragtone.chunking import chat_chunk
from ragtone.ingest.base import FetchResult
from ragtone.ingest.client import ToolCaller
from ragtone.ingest.parse import as_records, as_text, later_watermark, unix_days_ago
from ragtone.settings import ChatSource, Settings


class ChatConnector:
    name = "chat"

    def __init__(
        self,
        source: ChatSource,
        caller: ToolCaller,
        *,
        pause: float,
        default_days: int,
        checkpoints: CheckpointStore | None = None,
    ) -> None:
        self.source = source
        self.caller = caller
        self.pause = pause
        self.default_days = default_days
        self.checkpoints = checkpoints

    def _oldest(self, channel: str, *, backfill: bool) -> str:
        key = f"chat:{channel}"
        saved = self.checkpoints.get(key) if self.checkpoints is not None else None
        if backfill or not saved:
            days = self.source.channel_windows.get(channel)
            if days is None:
                days = self.default_days
            return unix_days_ago(days)
        return saved

    async def fetch(self, checkpoint: str | None, *, backfill: bool) -> FetchResult:
        chunks = []
        newest = checkpoint
        watermarks: dict[str, str] = {}
        for channel in self.source.channels:
            oldest = self._oldest(channel, backfill=backfill)
            history = await self.caller.call_tool(
                self.source.history_tool,
                {"channel_id": channel, "oldest": oldest},
            )
            messages = as_records(history, "messages", "results")
            thread_ids: set[str] = set()
            channel_newest = None
            for message in messages:
                message_id = str(message.get("ts") or message.get("id") or "")
                if not message_id:
                    continue
                thread_id = str(message.get("thread_ts") or message_id)
                if message.get("reply_count") or message.get("thread_ts"):
                    thread_ids.add(thread_id)
                chunk = chat_chunk(
                    message_id=message_id,
                    text=as_text(message.get("text") or message.get("body")),
                    channel=channel,
                    thread_id=thread_id,
                    url=str(message.get("permalink") or message.get("url") or ""),
                    created_at=str(message.get("ts") or message.get("created") or "") or None,
                    author=as_text(message.get("user") or message.get("author") or ""),
                )
                chunks.append(chunk)
                channel_newest = later_watermark(channel_newest, chunk.created_at)
            for thread_id in thread_ids:
                replies = await self.caller.call_tool(
                    self.source.replies_tool,
                    {"channel_id": channel, "message_ts": thread_id},
                )
                for message in as_records(replies, "messages", "replies"):
                    message_id = str(message.get("ts") or message.get("id") or "")
                    if not message_id:
                        continue
                    chunk = chat_chunk(
                        message_id=message_id,
                        text=as_text(message.get("text") or message.get("body")),
                        channel=channel,
                        thread_id=thread_id,
                        url=str(message.get("permalink") or message.get("url") or ""),
                        created_at=str(message.get("ts") or message.get("created") or "") or None,
                        author=as_text(message.get("user") or message.get("author") or ""),
                    )
                    chunks.append(chunk)
                    channel_newest = later_watermark(channel_newest, chunk.created_at)
                await asyncio.sleep(self.pause)
            if channel_newest:
                watermarks[f"chat:{channel}"] = channel_newest
                newest = later_watermark(newest, channel_newest)
            await asyncio.sleep(self.pause)
        return FetchResult(chunks=chunks, watermark=newest, watermarks=watermarks)


def build_chat(
    settings: Settings,
    callers: dict[str, ToolCaller],
    checkpoints: CheckpointStore | None = None,
) -> ChatConnector | None:
    if not settings.chat.enabled:
        return None
    return ChatConnector(
        settings.chat,
        callers[settings.chat.mcp],
        pause=settings.mcp_pause_seconds,
        default_days=settings.backfill_days,
        checkpoints=checkpoints,
    )
