from __future__ import annotations

import asyncio
from typing import Any

from ragtone.checkpoints import CheckpointStore
from ragtone.chunking import chat_chunk
from ragtone.ingest.base import FetchResult, Page, WorkRecord, fetch_all
from ragtone.ingest.client import ToolCaller
from ragtone.ingest.page import chat_next, paged_records, search_page
from ragtone.ingest.parse import as_text, later_watermark, unix_days_ago
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
        return await fetch_all(self, checkpoint, backfill=backfill)

    async def next_page(
        self,
        checkpoint: str | None,
        *,
        backfill: bool,
        cursor: dict[str, Any] | None,
    ) -> Page:
        channels = self.source.channels
        if not channels:
            return Page(done=True)
        state = dict(cursor or {"i": 0})
        index = int(state.get("i") or 0)
        if index >= len(channels):
            return Page(done=True)
        channel = channels[index]
        oldest = self._oldest(channel, backfill=backfill)
        extra = state.get("h") if isinstance(state.get("h"), dict) else None
        page = await search_page(
            self.caller,
            self.source.history_tool,
            {"channel_id": channel, "oldest": oldest},
            "messages",
            "results",
            next_args=chat_next,
            extra=extra,
        )
        if self.pause:
            await asyncio.sleep(self.pause)
        key = f"chat:{channel}"
        work: list[WorkRecord] = []
        threads = [str(item) for item in (state.get("t") or [])]
        seen = set(threads)
        for message in page.records:
            message_id = str(message.get("ts") or message.get("id") or "")
            if not message_id:
                continue
            thread_id = str(message.get("thread_ts") or message_id)
            if (message.get("reply_count") or message.get("thread_ts")) and thread_id not in seen:
                threads.append(thread_id)
                seen.add(thread_id)
            stamp = str(message.get("ts") or message.get("created") or "") or None
            work.append(
                WorkRecord(
                    ref=message_id,
                    payload={"kind": "message", "channel": channel, "message": message},
                    watermark=stamp,
                    checkpoint_key=key,
                )
            )
        if page.next_args:
            return Page(
                records=work,
                cursor={"i": index, "h": page.next_args, "t": threads},
                done=False,
            )
        for thread_id in threads:
            work.append(
                WorkRecord(
                    ref=f"{channel}:{thread_id}:replies",
                    payload={"kind": "thread", "channel": channel, "thread_id": thread_id},
                    watermark=None,
                    checkpoint_key=key,
                )
            )
        next_index = index + 1
        done = next_index >= len(channels)
        return Page(
            records=work,
            cursor=None if done else {"i": next_index},
            done=done,
        )

    async def materialize(self, record: WorkRecord) -> FetchResult:
        payload = record.payload
        channel = str(payload.get("channel") or "")
        key = record.checkpoint_key or f"chat:{channel}"
        if payload.get("kind") == "thread":
            thread_id = str(payload.get("thread_id") or "")
            replies = await paged_records(
                self.caller,
                self.source.replies_tool,
                {"channel_id": channel, "message_ts": thread_id},
                "messages",
                "replies",
                pause=self.pause,
                next_args=chat_next,
            )
            chunks = []
            newest = None
            for message in replies:
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
            if self.pause:
                await asyncio.sleep(self.pause)
            return FetchResult(
                chunks=chunks,
                watermark=newest,
                watermarks={key: newest} if newest else {},
            )
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        message_id = str(message.get("ts") or message.get("id") or record.ref)
        thread_id = str(message.get("thread_ts") or message_id)
        chunk = chat_chunk(
            message_id=message_id,
            text=as_text(message.get("text") or message.get("body")),
            channel=channel,
            thread_id=thread_id,
            url=str(message.get("permalink") or message.get("url") or ""),
            created_at=str(message.get("ts") or message.get("created") or "") or None,
            author=as_text(message.get("user") or message.get("author") or ""),
        )
        return FetchResult(
            chunks=[chunk],
            watermark=chunk.created_at,
            watermarks={key: chunk.created_at} if chunk.created_at else {},
        )


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
