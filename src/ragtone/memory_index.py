from __future__ import annotations

from typing import Sequence

from ragtone.embeddings import lexical_score
from ragtone.index import hit_from_source
from ragtone.models import Chunk, Filters, Hit


def _matches(doc: dict, filters: Filters) -> bool:
    if filters.source and doc.get("source") != filters.source:
        return False
    if filters.channel and doc.get("channel_or_space") != filters.channel:
        return False
    if filters.since and (doc.get("updated_at") or "") < filters.since:
        return False
    return True


def _haystack(doc: dict) -> str:
    return " ".join(
        str(doc.get(field) or "")
        for field in (
            "title",
            "text",
            "native_id",
            "parent_id",
            "thread_id",
            "channel_or_space",
        )
    )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


class InMemoryIndex:
    def __init__(self) -> None:
        self._docs: dict[str, dict] = {}
        self._vectors: dict[str, list[float]] = {}

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        for chunk, vector in zip(chunks, vectors):
            self._docs[chunk.id] = chunk.to_document()
            self._vectors[chunk.id] = list(vector)

    def search(
        self,
        query: str,
        vector: Sequence[float],
        filters: Filters,
        k: int = 8,
    ) -> list[Hit]:
        scored: list[Hit] = []
        for doc_id, doc in self._docs.items():
            if not _matches(doc, filters):
                continue
            dense = _cosine(vector, self._vectors[doc_id])
            lexical = lexical_score(query, _haystack(doc))
            score = 0.6 * dense + 0.4 * lexical
            scored.append(hit_from_source(doc_id, score, doc))
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[:k]

    def lexical_search(self, query: str, filters: Filters, k: int = 8) -> list[Hit]:
        scored: list[Hit] = []
        for doc_id, doc in self._docs.items():
            if not _matches(doc, filters):
                continue
            lexical = lexical_score(query, _haystack(doc))
            if lexical <= 0:
                continue
            scored.append(hit_from_source(doc_id, lexical, doc))
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[:k]

    def by_ids(self, ids: Sequence[str]) -> list[Hit]:
        hits: list[Hit] = []
        for doc_id in ids:
            doc = self._docs.get(doc_id)
            if doc is None:
                continue
            hits.append(hit_from_source(doc_id, 1.0, doc))
        return hits

    def by_thread(self, thread_id: str) -> list[Hit]:
        return self._where(lambda doc: doc.get("thread_id") == thread_id)

    def by_issue(self, key: str) -> list[Hit]:
        return self._where(
            lambda doc: doc.get("source") == "jira" and doc.get("parent_id") == key
        )

    def by_page(self, page_id: str) -> list[Hit]:
        return self._where(
            lambda doc: doc.get("source") == "confluence"
            and doc.get("parent_id") == page_id
        )

    def recent(self, *, source: str | None = "chat", k: int = 40) -> list[Hit]:
        hits = [
            hit_from_source(doc_id, 1.0, doc)
            for doc_id, doc in self._docs.items()
            if not source or doc.get("source") == source
        ]
        hits.sort(key=lambda hit: hit.updated_at or "", reverse=True)
        return hits[:k]

    def stats(self) -> dict:
        by_source: dict[str, int] = {}
        for doc in self._docs.values():
            source = str(doc.get("source") or "unknown")
            by_source[source] = by_source.get(source, 0) + 1
        return {"ok": True, "total": len(self._docs), "by_source": by_source}

    def _where(self, predicate) -> list[Hit]:
        hits = [
            hit_from_source(doc_id, 1.0, doc)
            for doc_id, doc in self._docs.items()
            if predicate(doc)
        ]
        hits.sort(key=lambda hit: hit.updated_at or hit.id)
        return hits
