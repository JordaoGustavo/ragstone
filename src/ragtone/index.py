from __future__ import annotations

import re
from typing import Any, Sequence

from elasticsearch import Elasticsearch, helpers

from ragtone.models import Chunk, Filters, Hit

EMBEDDING_FIELD = "embedding"
SEARCH_TIMEOUT = 10
_ISSUE_KEY = re.compile(r"\b([A-Za-z][A-Za-z0-9_]+-\d+)\b")
_ID_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{3,}$")


def filter_clauses(filters: Filters) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    if filters.source:
        clauses.append({"term": {"source": filters.source}})
    if filters.channel:
        clauses.append({"term": {"channel_or_space": filters.channel}})
    if filters.since:
        clauses.append({"range": {"updated_at": {"gte": filters.since}}})
    return clauses


def identifier_values(query: str) -> list[str]:
    stripped = query.strip()
    values: list[str] = []
    if stripped:
        values.append(stripped)
    for key in _ISSUE_KEY.findall(stripped):
        if key not in values:
            values.append(key)
    return values


def identifier_clauses(query: str) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    fields = (("native_id", 8.0), ("parent_id", 6.0), ("thread_id", 4.0))
    for value in identifier_values(query):
        for field, boost in fields:
            clauses.append(
                {
                    "term": {
                        field: {
                            "value": value,
                            "boost": boost,
                            "case_insensitive": True,
                        }
                    }
                }
            )
    token = query.strip()
    if _ID_TOKEN.fullmatch(token):
        clauses.append(
            {
                "prefix": {
                    "native_id": {
                        "value": token,
                        "boost": 3.0,
                        "case_insensitive": True,
                    }
                }
            }
        )
    return clauses


def _lexical_should(query: str) -> list[dict[str, Any]]:
    return [
        {
            "multi_match": {
                "query": query,
                "fields": ["title^2", "text"],
                "type": "best_fields",
                "fuzziness": "AUTO",
                "prefix_length": 1,
                "lenient": True,
            }
        },
        *identifier_clauses(query),
    ]


def _bool_query(should: list[dict[str, Any]], filters: Filters) -> dict[str, Any]:
    body_query: dict[str, Any] = {
        "bool": {
            "should": should,
            "minimum_should_match": 1,
        }
    }
    clauses = filter_clauses(filters)
    if clauses:
        body_query["bool"]["filter"] = clauses
    return body_query


def lexical_search_body(
    *,
    query: str,
    filters: Filters,
    k: int,
) -> dict[str, Any]:
    """Spotlight path: BM25 + identifiers, no kNN."""
    return {"size": k, "query": _bool_query(_lexical_should(query), filters)}


def hybrid_search_body(
    *,
    query: str,
    vector: Sequence[float],
    filters: Filters,
    k: int,
) -> dict[str, Any]:
    """MCP path: kNN-first hybrid with lexical identifiers as a backstop."""
    should: list[dict[str, Any]] = [
        {
            "knn": {
                "field": EMBEDDING_FIELD,
                "query_vector": list(vector),
                "k": k,
                "num_candidates": max(k * 8, 50),
                "boost": 2.0,
            }
        },
        *_lexical_should(query),
    ]
    return {"size": k, "query": _bool_query(should, filters)}


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
        return self._query_hits(body)

    def lexical_search(self, query: str, filters: Filters, k: int = 8) -> list[Hit]:
        return self._query_hits(lexical_search_body(query=query, filters=filters, k=k))

    def by_ids(self, ids: Sequence[str]) -> list[Hit]:
        if not ids:
            return []
        return self._query_hits(
            {
                "size": len(ids),
                "query": {"ids": {"values": list(ids)}},
            }
        )

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
        return self._query_hits(
            {
                "size": 100,
                "query": {"bool": {"filter": filters}},
                "sort": [{"updated_at": {"order": "asc", "unmapped_type": "date"}}],
            }
        )

    def _query_hits(self, body: dict[str, Any]) -> list[Hit]:
        kwargs: dict[str, Any] = {
            "index": self.index_name,
            "size": body["size"],
            "query": body["query"],
            "source_excludes": [EMBEDDING_FIELD],
        }
        if "sort" in body:
            kwargs["sort"] = body["sort"]
        response = self.es.options(request_timeout=SEARCH_TIMEOUT).search(**kwargs)
        return [
            hit_from_source(
                str(raw["_id"]),
                float(raw.get("_score") or 0),
                raw.get("_source") or {},
            )
            for raw in response["hits"]["hits"]
        ]
