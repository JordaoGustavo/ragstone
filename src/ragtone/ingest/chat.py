from __future__ import annotations

import asyncio

from ragtone.chunking import chat_chunk
from ragtone.ingest.base import FetchResult
from ragtone.ingest.client import ToolCaller
from ragtone.ingest.parse import as_records, as_text, later_watermark
from ragtone.settings import ChatSource, Settings


class ChatConnector:
    name = "chat"

    def __init__(
        self,
        source: ChatSource,
        caller: ToolCaller,
        *,
        pause: float,
    ) -> None:
        self.source = source
        self.caller = caller
        self.pause = pause

    async def fetch(self, checkpoint: str | None, *, backfill: bool) -> FetchResult:
        chunks = []
        newest = checkpoint
        for channel in self.source.channels:
            history = await self.caller.call_tool(
                self.source.history_tool,
                {"channel": channel, "oldest": checkpoint or "0"},
            )
            messages = as_records(history, "messages", "results")
            thread_ids: set[str] = set()
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
                newest = later_watermark(newest, chunk.created_at)
            for thread_id in thread_ids:
                replies = await self.caller.call_tool(
                    self.source.replies_tool,
                    {"channel": channel, "thread_ts": thread_id},
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
                    newest = later_watermark(newest, chunk.created_at)
                await asyncio.sleep(self.pause)
            await asyncio.sleep(self.pause)
        return FetchResult(chunks=chunks, watermark=newest)


def build_chat(settings: Settings, callers: dict[str, ToolCaller]) -> ChatConnector | None:
    if not settings.chat.enabled:
        return None
    return ChatConnector(
        settings.chat,
        callers[settings.chat.mcp],
        pause=settings.mcp_pause_seconds,
    )
