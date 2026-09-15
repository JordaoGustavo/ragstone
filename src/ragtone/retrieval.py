from __future__ import annotations

from typing import Protocol, Sequence

from ragtone.embeddings import Embedder
from ragtone.models import Filters, Hit
from ragtone.origin import Origins, origin_for


def root_ref(hit: Hit) -> str:
    return hit.thread_id or hit.parent_id or hit.native_id or hit.id


def is_root(hit: Hit) -> bool:
    if hit.source == "jira":
        return ":comment:" not in hit.native_id
    if hit.source == "confluence":
        parent = hit.parent_id or hit.native_id
        return hit.native_id == parent
    thread = hit.thread_id or hit.parent_id or hit.native_id
    return bool(thread) and hit.native_id == thread


def root_id(hit: Hit) -> str:
    return f"{hit.source}:{root_ref(hit)}"


class ChunkStore(Protocol):
    def upsert(self, chunks, vectors) -> None: ...

    def search(
        self,
        query: str,
        vector: Sequence[float],
        filters: Filters,
        k: int = 8,
    ) -> list[Hit]: ...

    def lexical_search(self, query: str, filters: Filters, k: int = 8) -> list[Hit]: ...

    def by_ids(self, ids: Sequence[str]) -> list[Hit]: ...

    def by_thread(self, thread_id: str) -> list[Hit]: ...

    def by_issue(self, key: str) -> list[Hit]: ...

    def by_page(self, page_id: str) -> list[Hit]: ...

    def recent(self, *, source: str | None = "chat", k: int = 40) -> list[Hit]: ...

    def stats(self) -> dict: ...


class RetrievalService:
    def __init__(
        self,
        store: ChunkStore,
        embedder: Embedder,
        origins: Origins | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.origins = origins or Origins()

    def _present(self, hit: Hit, *, text_limit: int | None = None) -> dict:
        data = hit.as_dict(text_limit=text_limit)
        data["url"] = origin_for(hit, self.origins)
        return data

    def _collapse_roots(self, hits: Sequence[Hit], k: int) -> list[Hit]:
        ordered: list[str] = []
        first: dict[str, Hit] = {}
        present: dict[str, Hit] = {}
        for hit in hits:
            key = root_id(hit)
            if is_root(hit):
                present[key] = hit
            if key in first:
                continue
            first[key] = hit
            ordered.append(key)
            if len(ordered) >= k:
                break
        missing = [key for key in ordered if key not in present]
        if missing:
            present.update({hit.id: hit for hit in self.store.by_ids(missing)})
        return [present.get(key) or first[key] for key in ordered]

    def search(
        self,
        query: str,
        *,
        source: str | None = None,
        channel: str | None = None,
        since: str | None = None,
        k: int = 8,
    ) -> list[dict]:
        """MCP path: embed the query and rank matching chunks by kNN/hybrid."""
        vector = self.embedder.embed([query])[0]
        hits = self.store.search(
            query,
            vector,
            Filters(source=source, channel=channel, since=since),
            k=k,
        )
        return [self._present(hit, text_limit=800) for hit in hits]

    def spotlight(
        self,
        query: str,
        *,
        source: str | None = None,
        channel: str | None = None,
        since: str | None = None,
        k: int = 12,
    ) -> list[dict]:
        """Spotlight path: lexical search collapsed to pages, issues, and threads."""
        hits = self.store.lexical_search(
            query,
            Filters(source=source, channel=channel, since=since),
            k=max(k * 4, 32),
        )
        return [self._present(hit, text_limit=800) for hit in self._collapse_roots(hits, k)]

    def thread(self, thread_id: str) -> list[dict]:
        return [self._present(hit) for hit in self.store.by_thread(thread_id)]

    def issue(self, key: str) -> list[dict]:
        return [self._present(hit) for hit in self.store.by_issue(key)]

    def page(self, page_id: str) -> list[dict]:
        hits = self.store.by_page(page_id)
        sections = [hit for hit in hits if not is_root(hit)]
        return [self._present(hit) for hit in (sections or hits)]

    def recent(
        self,
        *,
        source: str | None = "chat",
        k: int = 20,
        fallback: bool = True,
    ) -> list[dict]:
        raw = self.store.recent(source=source, k=max(k * 4, 40))
        if not raw and source and fallback:
            raw = self.store.recent(source=None, k=max(k * 4, 40))
        seen: set[str] = set()
        collapsed: list[dict] = []
        for hit in raw:
            key = hit.thread_id or hit.parent_id or hit.id
            if key in seen:
                continue
            seen.add(key)
            collapsed.append(self._present(hit, text_limit=220))
            if len(collapsed) >= k:
                break
        return collapsed

    def stats(self) -> dict:
        return self.store.stats()
