from __future__ import annotations

from typing import Any, Sequence

from elasticsearch import Elasticsearch, helpers

from ragtone.models import Chunk, Filters, Hit

EMBEDDING_FIELD = "embedding"


def filter_clauses(filters: Filters) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    if filters.source:
        clauses.append({"term": {"source": filters.source}})
    if filters.channel:
        clauses.append({"term": {"channel_or_space": filters.channel}})
    if filters.since:
        clauses.append({"range": {"updated_at": {"gte": filters.since}}})
    return clauses


def hybrid_search_body(
    *,
    query: str,
    vector: Sequence[float],
    filters: Filters,
    k: int,
) -> dict[str, Any]:
    clauses = filter_clauses(filters)
    lexical: dict[str, Any] = {
        "bool": {
            "must": [
                {
                    "multi_match": {
                        "query": query,
                        "fields": ["title^2", "text"],
                    }
                }
            ]
        }
    }
    if clauses:
        lexical["bool"]["filter"] = clauses
    knn: dict[str, Any] = {
        "field": EMBEDDING_FIELD,
        "query_vector": list(vector),
        "k": k,
        "num_candidates": max(k * 8, 50),
    }
    if clauses:
        knn["filter"] = {"bool": {"filter": clauses}}
    return {
        "size": k,
        "_source": {"excludes": [EMBEDDING_FIELD]},
        "retriever": {
            "rrf": {
                "retrievers": [
                    {"standard": {"query": lexical}},
                    {"knn": knn},
                ],
                "rank_window_size": max(k * 5, 20),
            }
        },
    }


def mappings(dims: int) -> dict[str, Any]:
    return {
        "properties": {
            "id": {"type": "keyword"},
            "source": {"type": "keyword"},
            "native_id": {"type": "keyword"},
            "text": {"type": "text"},
            "title": {"type": "text"},
            "url": {"type": "keyword"},
            "parent_id": {"type": "keyword"},
            "thread_id": {"type": "keyword"},
            "channel_or_space": {"type": "keyword"},
            "created_at": {"type": "date", "ignore_malformed": True},
            "updated_at": {"type": "date", "ignore_malformed": True},
            "authors": {"type": "keyword"},
            EMBEDDING_FIELD: {
                "type": "dense_vector",
                "dims": dims,
                "index": True,
                "similarity": "cosine",
            },
        }
    }


def hit_from_source(doc_id: str, score: float, source: dict[str, Any]) -> Hit:
    return Hit(
        id=doc_id,
        score=score,
        source=str(source.get("source") or ""),
        title=str(source.get("title") or ""),
        text=str(source.get("text") or ""),
        url=str(source.get("url") or ""),
        native_id=str(source.get("native_id") or ""),
        parent_id=str(source.get("parent_id") or ""),
        thread_id=str(source.get("thread_id") or ""),
        channel_or_space=str(source.get("channel_or_space") or ""),
        updated_at=source.get("updated_at"),
    )


class SearchIndex:
    def __init__(self, es: Elasticsearch, index_name: str, dims: int) -> None:
        self.es = es
        self.index_name = index_name
        self.dims = dims

    def ping(self) -> bool:
        try:
            return bool(self.es.ping())
        except Exception:
            return False

    def ensure_index(self) -> None:
        if self.es.indices.exists(index=self.index_name):
            return
        self.es.indices.create(
            index=self.index_name,
            mappings=mappings(self.dims),
        )

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must be the same length")
        actions = []
        for chunk, vector in zip(chunks, vectors):
            doc = chunk.to_document()
            doc[EMBEDDING_FIELD] = list(vector)
            actions.append(
                {
                    "_op_type": "index",
                    "_index": self.index_name,
                    "_id": chunk.id,
                    "_source": doc,
                }
            )
        if actions:
            helpers.bulk(self.es, actions)
            self.es.indices.refresh(index=self.index_name)

    def search(
        self,
        query: str,
        vector: Sequence[float],
        filters: Filters,
        k: int = 8,
    ) -> list[Hit]:
        body = hybrid_search_body(query=query, vector=vector, filters=filters, k=k)
        response = self.es.search(
            index=self.index_name,
            size=body["size"],
            retriever=body["retriever"],
            source_excludes=[EMBEDDING_FIELD],
        )
        return [
            hit_from_source(
                str(raw["_id"]),
                float(raw.get("_score") or 0),
                raw.get("_source") or {},
            )
            for raw in response["hits"]["hits"]
        ]

    def by_thread(self, thread_id: str) -> list[Hit]:
        return self._term("thread_id", thread_id)

    def by_issue(self, key: str) -> list[Hit]:
        return self._term("parent_id", key, extra={"term": {"source": "jira"}})

    def by_page(self, page_id: str) -> list[Hit]:
        return self._term("parent_id", page_id, extra={"term": {"source": "confluence"}})

    def recent(self, *, source: str | None = "chat", k: int = 40) -> list[Hit]:
        query: dict[str, Any]
        if source:
            query = {"bool": {"filter": [{"term": {"source": source}}]}}
        else:
            query = {"match_all": {}}
        response = self.es.search(
            index=self.index_name,
            size=k,
            query=query,
            sort=[{"updated_at": {"order": "desc", "unmapped_type": "date"}}],
            source_excludes=[EMBEDDING_FIELD],
        )
        return [
            hit_from_source(
                str(raw["_id"]),
                float(raw.get("_score") or 0),
                raw.get("_source") or {},
            )
            for raw in response["hits"]["hits"]
        ]

    def stats(self) -> dict[str, Any]:
        if not self.ping():
            return {"ok": False, "total": 0, "by_source": {}}
        try:
            response = self.es.search(
                index=self.index_name,
                size=0,
                query={"match_all": {}},
                aggregations={"by_source": {"terms": {"field": "source", "size": 12}}},
                track_total_hits=True,
                source=False,
            )
        except Exception:
            return {"ok": False, "total": 0, "by_source": {}}
        total = response["hits"]["total"]
        if isinstance(total, dict):
            total = int(total.get("value") or 0)
        buckets = response.get("aggregations", {}).get("by_source", {}).get("buckets", [])
        by_source = {str(item["key"]): int(item["doc_count"]) for item in buckets}
        return {"ok": True, "total": int(total), "by_source": by_source}

    def _term(
        self,
        field: str,
        value: str,
        extra: dict[str, Any] | None = None,
    ) -> list[Hit]:
        filters: list[dict[str, Any]] = [{"term": {field: value}}]
        if extra:
            filters.append(extra)
        response = self.es.search(
            index=self.index_name,
            size=100,
            query={"bool": {"filter": filters}},
            sort=[{"updated_at": {"order": "asc", "unmapped_type": "date"}}],
            source_excludes=[EMBEDDING_FIELD],
        )
        return [
            hit_from_source(
                str(raw["_id"]),
                float(raw.get("_score") or 0),
                raw.get("_source") or {},
            )
            for raw in response["hits"]["hits"]
        ]
