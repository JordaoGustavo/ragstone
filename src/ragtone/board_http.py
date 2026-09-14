from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from elasticsearch import Elasticsearch
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ragtone.admin import idle_job, snapshot
from ragtone.board import BoardStore, delete_walk, link_nodes, move_node, new_walk, pin_node, unlink_edge, unpin_node
from ragtone.checkpoints import CheckpointStore
from ragtone.embeddings import HashEmbedder, build_embedder
from ragtone.index import SearchIndex
from ragtone.ingest.run import IngestConfigError, with_worker
from ragtone.retrieval import RetrievalService
from ragtone.settings import Settings

log = logging.getLogger(__name__)
WEB = Path(__file__).parent / "web" / "board"


def _opt_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


class BoardContext:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = BoardStore(settings.board_path)
        self._retrieval: RetrievalService | None = None
        self._retrieval_failed = False
        self.job = idle_job()
        self._sync_task: asyncio.Task | None = None

    def live_index(self) -> SearchIndex | None:
        try:
            es_index = SearchIndex(
                Elasticsearch(
                    self.settings.elasticsearch_url,
                    request_timeout=1,
                    max_retries=0,
                ),
                self.settings.elasticsearch_index,
                self.settings.embed_dims,
            )
            if not es_index.ping():
                return None
            self._retrieval_failed = False
            return es_index
        except Exception:
            return None

    def admin_snapshot(self) -> dict:
        index = self.live_index()
        if index is None:
            stats = {"ok": False, "total": 0, "by_source": {}}
            recent: list[dict] = []
        else:
            stats = index.stats()
            recent = RetrievalService(index, HashEmbedder(8)).recent(source=None, k=20)
        return snapshot(
            self.settings,
            stats=stats,
            checkpoints=CheckpointStore(self.settings.checkpoint_path).all(),
            recent=recent,
            job=self.job,
        )

    def retrieval(self) -> RetrievalService | None:
        if self._retrieval is not None:
            return self._retrieval
        if self._retrieval_failed:
            return None
        try:
            es_index = SearchIndex(
                Elasticsearch(
                    self.settings.elasticsearch_url,
                    request_timeout=1,
                    max_retries=0,
                ),
                self.settings.elasticsearch_index,
                self.settings.embed_dims,
            )
            if not es_index.ping():
                self._retrieval_failed = True
                return None
            self._retrieval = RetrievalService(
                es_index,
                build_embedder(
                    self.settings.embedder,
                    self.settings.embed_model,
                    self.settings.embed_dims,
                ),
            )
            return self._retrieval
        except Exception:
            log.exception("retrieval unavailable")
            self._retrieval_failed = True
            return None


def _ctx(request: Request) -> BoardContext:
    return request.app.state.ctx


async def index(_request: Request) -> FileResponse:
    return FileResponse(WEB / "index.html")


async def get_board(request: Request) -> JSONResponse:
    return JSONResponse(_ctx(request).store.load().model_dump())


async def save_camera(request: Request) -> JSONResponse:
    body = await request.json()
    store = _ctx(request).store
    board = store.load()
    board.camera.x = float(body["x"])
    board.camera.y = float(body["y"])
    board.camera.zoom = float(body["zoom"])
    store.save(board)
    return JSONResponse(board.model_dump())


async def pin(request: Request) -> JSONResponse:
    body = await request.json()
    store = _ctx(request).store
    board = store.load()
    pin_node(board, body["hit"], x=_opt_float(body.get("x")), y=_opt_float(body.get("y")))
    store.save(board)
    return JSONResponse(board.model_dump())


async def link(request: Request) -> JSONResponse:
    body = await request.json()
    store = _ctx(request).store
    board = store.load()
    link_nodes(board, body["from_id"], body["to_id"])
    store.save(board)
    return JSONResponse(board.model_dump())


async def unlink(request: Request) -> JSONResponse:
    body = await request.json()
    store = _ctx(request).store
    board = store.load()
    try:
        unlink_edge(board, str(body["id"]))
    except KeyError:
        return JSONResponse({"error": "link not found"}, status_code=404)
    store.save(board)
    return JSONResponse(board.model_dump())


async def unpin(request: Request) -> JSONResponse:
    body = await request.json()
    store = _ctx(request).store
    board = store.load()
    try:
        unpin_node(board, str(body["id"]))
    except KeyError:
        return JSONResponse({"error": "card not found"}, status_code=404)
    store.save(board)
    return JSONResponse(board.model_dump())


async def patch_node(request: Request) -> JSONResponse:
    body = await request.json()
    store = _ctx(request).store
    board = store.load()
    move_node(board, body["id"], float(body["x"]), float(body["y"]))
    store.save(board)
    return JSONResponse(board.model_dump())


async def create_walk(request: Request) -> JSONResponse:
    body = await request.json()
    store = _ctx(request).store
    board = store.load()
    new_walk(board, str(body.get("name") or ""))
    store.save(board)
    return JSONResponse(board.model_dump())


async def activate_walk(request: Request) -> JSONResponse:
    body = await request.json()
    store = _ctx(request).store
    board = store.load()
    walk_id = str(body["id"])
    if not any(walk.id == walk_id for walk in board.walks):
        return JSONResponse({"error": "walk not found"}, status_code=404)
    board.active_walk_id = walk_id
    store.save(board)
    return JSONResponse(board.model_dump())


async def drop_walk(request: Request) -> JSONResponse:
    body = await request.json()
    store = _ctx(request).store
    board = store.load()
    try:
        delete_walk(board, str(body["id"]))
    except KeyError:
        return JSONResponse({"error": "walk not found"}, status_code=404)
    store.save(board)
    return JSONResponse(board.model_dump())


async def search(request: Request) -> JSONResponse:
    query = request.query_params.get("q", "").strip()
    source = request.query_params.get("source") or None
    retrieval = _ctx(request).retrieval()
    if retrieval is None:
        return JSONResponse(
            {"ok": False, "reason": "elasticsearch unreachable", "hits": []}
        )
    if not query:
        return JSONResponse({"ok": True, "hits": []})
    hits = retrieval.search(query, source=source, k=12)
    return JSONResponse({"ok": True, "hits": hits})


async def recent(request: Request) -> JSONResponse:
    retrieval = _ctx(request).retrieval()
    if retrieval is None:
        return JSONResponse(
            {"ok": False, "reason": "elasticsearch unreachable", "hits": []}
        )
    source = request.query_params.get("source") or "chat"
    hits = retrieval.recent(source=None if source == "all" else source, k=20)
    return JSONResponse({"ok": True, "hits": hits})


async def expand(request: Request) -> JSONResponse:
    kind = request.query_params.get("kind", "")
    ref = request.query_params.get("ref", "")
    retrieval = _ctx(request).retrieval()
    if retrieval is None:
        return JSONResponse({"ok": False, "items": []})
    if kind == "thread":
        items = retrieval.thread(ref)
    elif kind == "issue":
        items = retrieval.issue(ref)
    elif kind == "page":
        items = retrieval.page(ref)
    else:
        return JSONResponse({"ok": False, "items": []}, status_code=400)
    return JSONResponse({"ok": True, "items": items})


def _sync_names(ctx: BoardContext, name: str) -> tuple[list[str] | None, JSONResponse | None]:
    payload = ctx.admin_snapshot()
    rows = {row["name"]: row for row in payload["connectors"]}
    if name == "all":
        ready = [row["name"] for row in payload["connectors"] if row["can_sync"]]
        if not ready:
            return None, JSONResponse(
                {"error": "nenhum conector pronto para atualizar"},
                status_code=400,
            )
        return ready, None
    row = rows.get(name)
    if row is None:
        return None, JSONResponse({"error": "conector desconhecido"}, status_code=400)
    if not row["enabled"]:
        return None, JSONResponse(
            {"error": f"{name} desligado em ragtone.yaml"},
            status_code=400,
        )
    if not row["mcp_configured"]:
        return None, JSONResponse(
            {"error": f"{name} precisa do MCP configurado em foundation_mcps"},
            status_code=400,
        )
    return [name], None


async def _run_sync(ctx: BoardContext, label: str, names: list[str], *, backfill: bool) -> None:
    chunks = 0

    async def job(worker) -> None:
        nonlocal chunks
        if len(names) == 1:
            chunks = await worker.poll_named(names[0], backfill=backfill)
        elif backfill:
            chunks = await worker.backfill()
        else:
            chunks = await worker.poll_once()

    try:
        await with_worker(ctx.settings, job, names=names)
        ctx.job = {
            "status": "ok",
            "connector": label,
            "backfill": backfill,
            "chunks": chunks,
            "error": None,
        }
    except IngestConfigError as exc:
        ctx.job = {
            "status": "error",
            "connector": label,
            "backfill": backfill,
            "chunks": chunks,
            "error": str(exc),
        }
    except Exception as exc:
        log.exception("admin sync failed")
        ctx.job = {
            "status": "error",
            "connector": label,
            "backfill": backfill,
            "chunks": chunks,
            "error": str(exc),
        }


async def get_admin(request: Request) -> JSONResponse:
    return JSONResponse(_ctx(request).admin_snapshot())


async def start_sync(request: Request) -> JSONResponse:
    ctx = _ctx(request)
    body = await request.json()
    name = str(body.get("name") or body.get("connector") or "").strip()
    backfill = bool(body.get("backfill"))
    if name not in {"jira", "confluence", "chat", "all"}:
        return JSONResponse({"error": "conector desconhecido"}, status_code=400)
    names, error = _sync_names(ctx, name)
    if error is not None:
        return error
    assert names is not None
    if ctx.job.get("status") == "running":
        return JSONResponse({"error": "já tem uma atualização em curso"}, status_code=409)
    ctx.job = {
        "status": "running",
        "connector": name,
        "backfill": backfill,
        "chunks": 0,
        "error": None,
    }
    ctx._sync_task = asyncio.create_task(_run_sync(ctx, name, names, backfill=backfill))
    return JSONResponse(ctx.admin_snapshot())


def create_app(settings: Settings) -> Starlette:
    routes = [
        Route("/", index),
        Route("/api/board", get_board, methods=["GET"]),
        Route("/api/board/camera", save_camera, methods=["POST"]),
        Route("/api/board/pin", pin, methods=["POST"]),
        Route("/api/board/link", link, methods=["POST"]),
        Route("/api/board/unlink", unlink, methods=["POST"]),
        Route("/api/board/unpin", unpin, methods=["POST"]),
        Route("/api/board/node", patch_node, methods=["POST"]),
        Route("/api/board/walk", create_walk, methods=["POST"]),
        Route("/api/board/walk/active", activate_walk, methods=["POST"]),
        Route("/api/board/walk/delete", drop_walk, methods=["POST"]),
        Route("/api/search", search, methods=["GET"]),
        Route("/api/recent", recent, methods=["GET"]),
        Route("/api/expand", expand, methods=["GET"]),
        Route("/api/admin", get_admin, methods=["GET"]),
        Route("/api/admin/sync", start_sync, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(WEB)), name="static"),
    ]
    app = Starlette(routes=routes)
    app.state.ctx = BoardContext(settings)
    return app


def run_board(settings: Settings) -> None:
    if settings.board_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("board must bind localhost only")
    import uvicorn

    log.info("board listening on http://%s:%s", settings.board_host, settings.board_port)
    uvicorn.run(
        create_app(settings),
        host=settings.board_host,
        port=settings.board_port,
        log_level="info",
    )
