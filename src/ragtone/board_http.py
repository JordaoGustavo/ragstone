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
from ragtone.board import (
    Board,
    BoardNode,
    BoardStore,
    apply_unreads,
    capture_seen,
    delete_walk,
    entity_key,
    link_nodes,
    move_node,
    new_walk,
    pin_node,
    unlink_edge,
    unpin_node,
)
from ragtone.checkpoints import CheckpointStore
from ragtone.embeddings import build_embedder
from ragtone.index import SearchIndex
from ragtone.ingest.queue import JobQueue
from ragtone.ingest.run import IngestConfigError, with_worker
from ragtone.retrieval import RetrievalService
from ragtone.settings import Settings
from ragtone.watch_preview import peek_source
from ragtone.watches import SOURCES, WatchStore, parse_backfill_days, parse_targets

log = logging.getLogger(__name__)
WEB = Path(__file__).parent / "web" / "board"


def _opt_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


class BoardContext:
    def __init__(
        self,
        settings: Settings,
        retrieval: RetrievalService | None = None,
    ) -> None:
        self.settings = settings
        self.store = BoardStore(settings.board_path)
        self._retrieval = retrieval
        self._retrieval_failed = False
        self.job = idle_job()
        self._sync_task: asyncio.Task | None = None
        self.watches = WatchStore(settings.watch_path)
        self._queue: JobQueue | None = None

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

    def jobs(self) -> JobQueue | None:
        if self._queue is not None:
            return self._queue
        index = self.live_index()
        if index is None:
            return None
        queue = JobQueue.elasticsearch(index.es, self.settings.elasticsearch_index)
        queue.ensure()
        self._queue = queue
        return queue

    def admin_snapshot(self) -> dict:
        index = self.live_index()
        if index is None:
            stats = {"ok": False, "total": 0, "by_source": {}}
        else:
            stats = index.stats()
        queue = self.jobs()
        job = queue.admin_job() if queue is not None else self.job
        return snapshot(
            self.settings,
            stats=stats,
            checkpoints=CheckpointStore(self.settings.checkpoint_path).all(),
            recent=[],
            job=job,
            watches=self.watches.resolved(self.settings),
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
                    url=self.settings.embed_url,
                    token=self.settings.embed_token,
                ),
            )
            return self._retrieval
        except Exception:
            log.exception("retrieval unavailable")
            self._retrieval_failed = True
            return None


def _ctx(request: Request) -> BoardContext:
    return request.app.state.ctx


def hits_for_node(retrieval: RetrievalService, node: BoardNode) -> list[dict]:
    key = entity_key(node)
    if key is None:
        return []
    kind, ref = key
    if kind == "issue":
        return retrieval.issue(ref)
    if kind == "page":
        return retrieval.page(ref)
    return retrieval.thread(ref)


def _lookup(ctx: BoardContext, node: BoardNode) -> list[dict]:
    retrieval = ctx.retrieval()
    if retrieval is None:
        return []
    return hits_for_node(retrieval, node)


def _refresh_unreads(ctx: BoardContext, board: Board, *, persist: bool = True) -> Board:
    changed = apply_unreads(board, lambda node: _lookup(ctx, node))
    if persist and changed:
        ctx.store.save(board)
    return board


def _load_board(ctx: BoardContext) -> Board:
    return _refresh_unreads(ctx, ctx.store.load())


async def index(_request: Request) -> FileResponse:
    return FileResponse(WEB / "index.html")


async def get_board(request: Request) -> JSONResponse:
    return JSONResponse(_load_board(_ctx(request)).model_dump())


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
    ctx = _ctx(request)
    store = ctx.store
    board = store.load()
    known = {node.id for node in board.nodes}
    pin_node(board, body["hit"], x=_opt_float(body.get("x")), y=_opt_float(body.get("y")))
    added = next((node for node in board.nodes if node.id not in known), None)
    if added is not None:
        capture_seen(added, _lookup(ctx, added))
    store.save(board)
    return JSONResponse(_refresh_unreads(ctx, board).model_dump())


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
    try:
        k = int(request.query_params.get("k") or 20)
    except ValueError:
        k = 20
    k = max(1, min(k, 80))
    source = request.query_params.get("source")
    if source is None or source == "":
        hits = retrieval.recent(source="chat", k=k)
    elif source == "all":
        hits = retrieval.recent(source=None, k=k)
    else:
        hits = retrieval.recent(source=source, k=k, fallback=False)
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


def _requested_names(body: dict) -> tuple[list[str] | None, JSONResponse | None]:
    raw = body.get("names")
    if isinstance(raw, list):
        names: list[str] = []
        for item in raw:
            name = str(item).strip()
            if name not in {"jira", "confluence", "chat"}:
                return None, JSONResponse({"error": "conector desconhecido"}, status_code=400)
            if name not in names:
                names.append(name)
        if not names:
            return None, JSONResponse(
                {"error": "escolhe pelo menos um conector"},
                status_code=400,
            )
        return names, None
    name = str(body.get("name") or body.get("connector") or "").strip()
    if name not in {"jira", "confluence", "chat", "all"}:
        return None, JSONResponse({"error": "conector desconhecido"}, status_code=400)
    return [name], None


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
    if not row.get("watching"):
        labels = {
            "jira": "sem projetos/boards para olhar",
            "confluence": "sem espaços ou páginas para olhar",
            "chat": "sem canais para olhar",
        }
        return None, JSONResponse({"error": f"{name} {labels[name]}"}, status_code=400)
    return [name], None


async def _run_sync(ctx: BoardContext, names: list[str]) -> None:
    queue = ctx.jobs()
    if queue is None:
        ctx.job = {**idle_job(), "status": "error", "error": "Elasticsearch fora"}
        return
    if not queue.try_lease("board"):
        ctx.job = queue.admin_job()
        return
    try:
        await with_worker(ctx.settings, lambda worker: worker.drain(), names=names, queue=queue)
        ctx.job = queue.admin_job()
    except asyncio.CancelledError:
        ctx.job = queue.admin_job()
        raise
    except IngestConfigError as exc:
        ctx.job = {**queue.admin_job(), "status": "error", "error": str(exc)}
    except Exception as exc:
        log.exception("admin sync failed")
        ctx.job = {**queue.admin_job(), "status": "error", "error": str(exc)}
    finally:
        queue.release_lease("board")
        _refresh_unreads(ctx, ctx.store.load())


async def get_admin(request: Request) -> JSONResponse:
    return JSONResponse(_ctx(request).admin_snapshot())


async def start_sync(request: Request) -> JSONResponse:
    ctx = _ctx(request)
    body = await request.json()
    backfill = bool(body.get("backfill"))
    days = None
    if "backfill_days" in body:
        backfill = True
        try:
            days = parse_backfill_days(body.get("backfill_days"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    requested, error = _requested_names(body)
    if error is not None:
        return error
    assert requested is not None
    names: list[str] = []
    for name in requested:
        resolved, error = _sync_names(ctx, name)
        if error is not None:
            return error
        assert resolved is not None
        for item in resolved:
            if item not in names:
                names.append(item)
    if ctx.job.get("status") == "running":
        return JSONResponse({"error": "já tem uma atualização em curso"}, status_code=409)
    queue = ctx.jobs()
    if queue is None:
        return JSONResponse({"error": "Elasticsearch fora"}, status_code=503)
    busy = [item for item in names if queue.active_for(item)]
    if busy:
        return JSONResponse({"error": "já tem uma atualização em curso"}, status_code=409)
    for item in names:
        queue.create_run(item, backfill=backfill, backfill_days=days)
    ctx.job = queue.admin_job()
    if ctx.live_index() is not None:
        ctx._sync_task = asyncio.create_task(_run_sync(ctx, names))
    return JSONResponse(ctx.admin_snapshot())


async def stop_sync(request: Request) -> JSONResponse:
    ctx = _ctx(request)
    queue = ctx.jobs()
    cancelled: list = []
    if queue is not None:
        cancelled = queue.cancel_active()
    task = ctx._sync_task
    running_task = task is not None and not task.done()
    if running_task:
        task.cancel()
    if not cancelled and not running_task and ctx.job.get("status") != "running":
        return JSONResponse({"error": "nada em curso"}, status_code=409)
    if queue is not None:
        ctx.job = queue.admin_job()
    elif ctx.job.get("status") == "running":
        ctx.job = {**ctx.job, "status": "cancelled"}
    return JSONResponse(ctx.admin_snapshot())


async def save_watches(request: Request) -> JSONResponse:
    ctx = _ctx(request)
    body = await request.json()
    name = str(body.get("name") or "").strip()
    try:
        items = parse_targets(name, body.get("items"))
        ctx.watches.set(name, items)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(ctx.admin_snapshot())


async def retry_dlq(request: Request) -> JSONResponse:
    ctx = _ctx(request)
    queue = ctx.jobs()
    if queue is None:
        return JSONResponse({"error": "Elasticsearch fora"}, status_code=503)
    body = await request.json()
    try:
        if body.get("all"):
            queue.retry_all_dlq()
        else:
            queue.retry_item(str(body.get("id") or ""))
    except KeyError:
        return JSONResponse({"error": "item não encontrado"}, status_code=404)
    ctx.job = queue.admin_job()
    names = [run.connector for run in queue.active_runs()]
    if ctx.live_index() is not None and names:
        ctx._sync_task = asyncio.create_task(_run_sync(ctx, names))
    return JSONResponse(ctx.admin_snapshot())


async def mark_seen(request: Request) -> JSONResponse:
    body = await request.json()
    ctx = _ctx(request)
    store = ctx.store
    board = store.load()
    node = board.node(str(body.get("id") or ""))
    if node is None:
        return JSONResponse({"error": "card not found"}, status_code=404)
    capture_seen(node, _lookup(ctx, node))
    store.save(board)
    return JSONResponse(_refresh_unreads(ctx, board).model_dump())


async def peek_watch(request: Request) -> JSONResponse:
    ctx = _ctx(request)
    body = await request.json()
    name = str(body.get("name") or "").strip()
    raw = str(body.get("ref") or body.get("value") or "")
    if name not in SOURCES:
        return JSONResponse({"error": "conector desconhecido"}, status_code=400)
    if len(raw) > 2000:
        return JSONResponse({"error": "texto grande demais"}, status_code=400)
    data = await peek_source(ctx.settings, name, raw)
    if not data.get("id"):
        return JSONResponse(data, status_code=400)
    return JSONResponse(data)


def create_app(
    settings: Settings,
    retrieval: RetrievalService | None = None,
) -> Starlette:
    routes = [
        Route("/", index),
        Route("/api/board", get_board, methods=["GET"]),
        Route("/api/board/camera", save_camera, methods=["POST"]),
        Route("/api/board/pin", pin, methods=["POST"]),
        Route("/api/board/link", link, methods=["POST"]),
        Route("/api/board/unlink", unlink, methods=["POST"]),
        Route("/api/board/unpin", unpin, methods=["POST"]),
        Route("/api/board/node", patch_node, methods=["POST"]),
        Route("/api/board/seen", mark_seen, methods=["POST"]),
        Route("/api/board/walk", create_walk, methods=["POST"]),
        Route("/api/board/walk/active", activate_walk, methods=["POST"]),
        Route("/api/board/walk/delete", drop_walk, methods=["POST"]),
        Route("/api/search", search, methods=["GET"]),
        Route("/api/recent", recent, methods=["GET"]),
        Route("/api/expand", expand, methods=["GET"]),
        Route("/api/admin", get_admin, methods=["GET"]),
        Route("/api/admin/sync", start_sync, methods=["POST"]),
        Route("/api/admin/sync/stop", stop_sync, methods=["POST"]),
        Route("/api/admin/dlq/retry", retry_dlq, methods=["POST"]),
        Route("/api/admin/watches", save_watches, methods=["POST"]),
        Route("/api/admin/peek", peek_watch, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(WEB)), name="static"),
    ]
    app = Starlette(routes=routes)
    app.state.ctx = BoardContext(settings, retrieval=retrieval)
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
