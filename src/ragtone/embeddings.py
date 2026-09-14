from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Protocol, Sequence


class Embedder(Protocol):
    dims: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def l2_normalize(values: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


class HashEmbedder:
    """Deterministic stand-in so tests never download a model."""

    def __init__(self, dims: int = 384) -> None:
        self.dims = dims

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        raw: list[float] = []
        while len(raw) < self.dims:
            for byte in seed:
                raw.append((byte / 127.5) - 1.0)
                if len(raw) >= self.dims:
                    break
            seed = hashlib.sha256(seed).digest()
        return l2_normalize(raw)


class FastEmbedEmbedder:
    def __init__(self, model: str, dims: int) -> None:
        from fastembed import TextEmbedding

        self.dims = dims
        self._model = TextEmbedding(model_name=model)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for item in self._model.embed(list(texts)):
            values = [float(v) for v in item]
            if len(values) != self.dims:
                raise ValueError(
                    f"Embedding size {len(values)} does not match configured dims {self.dims}"
                )
            vectors.append(l2_normalize(values))
        return vectors


class HttpEmbedder:
    """Client for a ragtone embed server running on another machine."""

    def __init__(
        self,
        url: str,
        dims: int,
        token: str = "",
        *,
        timeout: float = 120.0,
        batch: int = 32,
        client: Any | None = None,
    ) -> None:
        if not url.strip():
            raise ValueError("embedder http needs embed_url")
        self.dims = dims
        self._base = url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._batch = batch
        self._client = client

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        items = list(texts)
        for start in range(0, len(items), self._batch):
            payload = {"texts": items[start : start + self._batch]}
            data = self._post("/embed", payload)
            vectors = data.get("vectors")
            if not isinstance(vectors, list) or len(vectors) != len(payload["texts"]):
                raise ValueError("embed server returned the wrong number of vectors")
            for vector in vectors:
                values = [float(v) for v in vector]
                if len(values) != self.dims:
                    raise ValueError(
                        f"Embedding size {len(values)} does not match configured dims {self.dims}"
                    )
                out.append(l2_normalize(values))
        return out

    def _post(self, path: str, payload: dict) -> dict:
        import httpx

        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._client is not None:
            response = self._client.post(path, json=payload, headers=headers)
        else:
            response = httpx.post(
                f"{self._base}{path}",
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
        if response.status_code == 401:
            raise ValueError("embed server rejected the token")
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("embed server returned invalid json")
        return data


def build_embedder(
    kind: str,
    model: str,
    dims: int,
    *,
    url: str = "",
    token: str = "",
) -> Embedder:
    if kind == "hash":
        return HashEmbedder(dims)
    if kind == "fastembed":
        return FastEmbedEmbedder(model, dims)
    if kind == "http":
        return HttpEmbedder(url, dims, token)
    raise ValueError(f"Unknown embedder {kind!r}")


_TOKEN = re.compile(r"[a-z0-9]+", re.I)


def lexical_score(query: str, text: str) -> float:
    q = set(_TOKEN.findall(query.lower()))
    if not q:
        return 0.0
    t = set(_TOKEN.findall(text.lower()))
    return len(q & t) / len(q)
