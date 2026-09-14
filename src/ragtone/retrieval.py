from __future__ import annotations

from typing import Protocol, Sequence

from ragtone.embeddings import Embedder
from ragtone.models import Filters, Hit


class ChunkStore(Protocol):
    def upsert(self, chunks, vectors) -> None: ...

    def search(
        self,
        query: str,
        vector: Sequence[float],
        filters: Filters,
        k: int = 8,
    ) -> list[Hit]: ...

    def by_thread(self, thread_id: str) -> list[Hit]: ...

    def by_issue(self, key: str) -> list[Hit]: ...

    def by_page(self, page_id: str) -> list[Hit]: ...

    def recent(self, *, source: str | None = "chat", k: int = 40) -> list[Hit]: ...

    def stats(self) -> dict: ...


class RetrievalService:
    def __init__(self, store: ChunkStore, embedder: Embedder) -> None:
        self.store = store
        self.embedder = embedder

    def search(
        self,
        query: str,
        *,
        source: str | None = None,
        channel: str | None = None,
        since: str | None = None,
        k: int = 8,
    ) -> list[dict]:
        vector = self.embedder.embed([query])[0]
        hits = self.store.search(
            query,
            vector,
            Filters(source=source, channel=channel, since=since),
            k=k,
        )
        return [hit.as_dict(text_limit=800) for hit in hits]

    def thread(self, thread_id: str) -> list[dict]:
        return [hit.as_dict() for hit in self.store.by_thread(thread_id)]

    def issue(self, key: str) -> list[dict]:
        return [hit.as_dict() for hit in self.store.by_issue(key)]

    def page(self, page_id: str) -> list[dict]:
        return [hit.as_dict() for hit in self.store.by_page(page_id)]

    def recent(self, *, source: str | None = "chat", k: int = 20) -> list[dict]:
        raw = self.store.recent(source=source, k=max(k * 4, 40))
        if not raw and source:
            raw = self.store.recent(source=None, k=max(k * 4, 40))
        seen: set[str] = set()
        collapsed: list[dict] = []
        for hit in raw:
            key = hit.thread_id or hit.parent_id or hit.id
            if key in seen:
                continue
            seen.add(key)
            collapsed.append(hit.as_dict(text_limit=220))
            if len(collapsed) >= k:
                break
        return collapsed

    def stats(self) -> dict:
        return self.store.stats()
