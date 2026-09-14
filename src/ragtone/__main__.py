from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextlib import AsyncExitStack
from pathlib import Path

from ragtone.board_http import run_board
from ragtone.embeddings import build_embedder
from ragtone.ingest.client import FoundationClient
from ragtone.ingest.run import IngestConfigError, open_index, with_worker
from ragtone.ingest.worker import IngestWorker
from ragtone.mcp_server import build_mcp, run_mcp
from ragtone.retrieval import RetrievalService
from ragtone.settings import Settings, load_settings

log = logging.getLogger(__name__)


def _index(settings: Settings):
    return open_index(settings)


def _embedder(settings: Settings):
    return build_embedder(settings.embedder, settings.embed_model, settings.embed_dims)


def _run_worker(settings: Settings, fn) -> None:
    try:
        asyncio.run(with_worker(settings, fn))
    except IngestConfigError as exc:
        raise SystemExit(str(exc)) from exc


def cmd_ping(settings: Settings) -> None:
    index = _index(settings)
    ok = index.ping()
    print("elasticsearch:", "ok" if ok else "unreachable", settings.elasticsearch_url)
    if not ok:
        raise SystemExit(1)


def cmd_ensure_index(settings: Settings) -> None:
    index = _index(settings)
    if not index.ping():
        raise SystemExit(f"Elasticsearch not reachable at {settings.elasticsearch_url}")
    index.ensure_index()
    print("index ready:", settings.elasticsearch_index)


def cmd_serve(settings: Settings) -> None:
    index = _index(settings)
    if not index.ping():
        raise SystemExit(f"Elasticsearch not reachable at {settings.elasticsearch_url}")
    index.ensure_index()
    retrieval = RetrievalService(index, _embedder(settings))
    run_mcp(build_mcp(retrieval), settings.mcp_host, settings.mcp_port)


def cmd_board(settings: Settings) -> None:
    run_board(settings)


def cmd_ingest(settings: Settings, *, backfill: bool) -> None:
    async def run(worker: IngestWorker) -> None:
        if backfill:
            await worker.backfill()
        else:
            await worker.poll_once()

    _run_worker(settings, run)


def cmd_sync(settings: Settings) -> None:
    _run_worker(settings, lambda worker: worker.run_loop())


async def _list_tools(settings: Settings) -> dict[str, list[dict[str, str]]]:
    listed: dict[str, list[dict[str, str]]] = {}
    async with AsyncExitStack() as stack:
        for spec in settings.foundation_mcps:
            client = await stack.enter_async_context(FoundationClient(spec))
            listed[spec.name] = await client.list_tools()
    return listed


def cmd_tools(settings: Settings) -> None:
    if not settings.foundation_mcps:
        raise SystemExit("No foundation_mcps configured in ragtone.yaml")
    print(json.dumps(asyncio.run(_list_tools(settings)), indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="ragtone")
    parser.add_argument("--config", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping", help="Check Elasticsearch")
    sub.add_parser("ensure-index", help="Create the Elasticsearch index if missing")
    sub.add_parser("serve", help="Run MCP2 on 127.0.0.1 for Claude/OpenCode")
    ingest = sub.add_parser("ingest", help="Pull from Foundation MCPs once")
    ingest.add_argument("--backfill", action="store_true")
    sub.add_parser("sync", help="Poll Foundation MCPs in a loop")
    sub.add_parser("tools", help="List tools on configured Foundation MCPs")
    sub.add_parser("board", help="Open the thread canvas on 127.0.0.1")
    args = parser.parse_args(argv)
    settings = load_settings(args.config)

    if args.command == "ping":
        cmd_ping(settings)
    elif args.command == "ensure-index":
        cmd_ensure_index(settings)
    elif args.command == "serve":
        cmd_serve(settings)
    elif args.command == "ingest":
        cmd_ingest(settings, backfill=args.backfill)
    elif args.command == "sync":
        cmd_sync(settings)
    elif args.command == "tools":
        cmd_tools(settings)
    elif args.command == "board":
        cmd_board(settings)
    else:
        parser.error(args.command)


if __name__ == "__main__":
    main()
