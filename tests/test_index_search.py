from __future__ import annotations

import pytest
from elasticsearch import Elasticsearch

from ragtone.chunking import jira_chunks
from ragtone.embeddings import HashEmbedder
from ragtone.index import SearchIndex
from ragtone.models import Filters


def _live_es():
    try:
        es = Elasticsearch("http://127.0.0.1:9200", request_timeout=2, max_retries=0)
        if not es.ping():
            pytest.skip("elasticsearch down")
        return es
    except Exception:
        pytest.skip("elasticsearch down")


def test_live_hybrid_search_finds_issue_key_without_rrf() -> None:
    es = _live_es()
    name = "ragtone_search_probe"
    if es.indices.exists(index=name):
        es.indices.delete(index=name)
    index = SearchIndex(es, name, 384)
    index.ensure_index()
    embedder = HashEmbedder(384)
    chunk = jira_chunks(
        key="ABC-12",
        summary="SSO timeout",
        description="gateway login",
    )[0]
    index.upsert([chunk], embedder.embed([chunk.text]))
    hits = index.search("ABC-12", embedder.embed(["ABC-12"])[0], Filters(), k=8)
    assert [hit.native_id for hit in hits] == ["ABC-12"]
    filtered = index.search(
        "SSO",
        embedder.embed(["SSO"])[0],
        Filters(source="chat"),
        k=8,
    )
    assert filtered == []
