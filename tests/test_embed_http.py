from __future__ import annotations

import math

import pytest
from starlette.testclient import TestClient

from ragtone.embed_http import create_embed_app, require_embed_bind
from ragtone.embeddings import HashEmbedder, HttpEmbedder, build_embedder


def test_embed_server_refuses_lan_without_token() -> None:
    with pytest.raises(ValueError, match="embed_token"):
        require_embed_bind("0.0.0.0", "")


def test_embed_server_allows_localhost_without_token() -> None:
    require_embed_bind("127.0.0.1", "")


def test_embed_server_allows_lan_with_token() -> None:
    require_embed_bind("0.0.0.0", "secret")


def test_http_embedder_matches_local_hash() -> None:
    local = HashEmbedder(8)
    app = create_embed_app(local, token="secret", model="hash")
    client = TestClient(app)
    remote = HttpEmbedder("http://embed", 8, "secret", client=client)
    texts = ["login timeout", "unrelated"]
    for got, expected in zip(remote.embed(texts), local.embed(texts), strict=True):
        assert got == pytest.approx(expected, rel=1e-6, abs=1e-6)
    assert math.isclose(math.sqrt(sum(v * v for v in remote.embed(["x"])[0])), 1.0, rel_tol=1e-6)
    health = client.get("/health").json()
    assert health == {"ok": True, "dims": 8, "model": "hash"}


def test_http_embedder_rejects_bad_token() -> None:
    app = create_embed_app(HashEmbedder(8), token="secret", model="hash")
    remote = HttpEmbedder("http://embed", 8, "wrong", client=TestClient(app))
    with pytest.raises(ValueError, match="token"):
        remote.embed(["hello"])


def test_build_embedder_http_needs_url() -> None:
    with pytest.raises(ValueError, match="embed_url"):
        build_embedder("http", "hash", 8)
