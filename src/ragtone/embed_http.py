from __future__ import annotations

import hmac
import logging
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ragtone.embeddings import Embedder

log = logging.getLogger(__name__)
LOCALHOST = {"127.0.0.1", "localhost", "::1"}


def require_embed_bind(host: str, token: str) -> None:
    if host in LOCALHOST:
        return
    if not token:
        raise ValueError("embed server on the LAN needs embed_token")


def _authorized(header: str, token: str) -> bool:
    expected = f"Bearer {token}".encode()
    got = header.encode()
    if len(got) != len(expected):
        return False
    return hmac.compare_digest(got, expected)


def create_embed_app(embedder: Embedder, *, token: str = "", model: str = "") -> Starlette:
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "dims": embedder.dims, "model": model})

    async def embed(request: Request) -> JSONResponse:
        if token and not _authorized(request.headers.get("authorization") or "", token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body: Any = await request.json()
        except Exception:
            return JSONResponse({"error": "json inválido"}, status_code=400)
        texts = body.get("texts") if isinstance(body, dict) else None
        if not isinstance(texts, list) or any(not isinstance(item, str) for item in texts):
            return JSONResponse({"error": "texts precisa ser uma lista de strings"}, status_code=400)
        vectors = embedder.embed(texts)
        return JSONResponse({"vectors": vectors, "dims": embedder.dims, "model": model})

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/embed", embed, methods=["POST"]),
        ]
    )


def run_embed_server(
    embedder: Embedder,
    host: str,
    port: int,
    *,
    token: str = "",
    model: str = "",
) -> None:
    require_embed_bind(host, token)
    import uvicorn

    log.info("embed listening on http://%s:%s/embed", host, port)
    uvicorn.run(
        create_embed_app(embedder, token=token, model=model),
        host=host,
        port=port,
        log_level="info",
    )
