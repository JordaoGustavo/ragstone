from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack

from elasticsearch import Elasticsearch

from ragtone.checkpoints import CheckpointStore
from ragtone.embeddings import build_embedder
from ragtone.index import SearchIndex
from ragtone.ingest.client import FoundationClient
from ragtone.ingest.worker import IngestWorker, build_connectors
from ragtone.settings import Settings
from ragtone.watches import WatchStore, overlay_settings

SOURCES = ("jira", "confluence", "chat")


class IngestConfigError(Exception):
    """Enabled source is missing its Foundation MCP."""


def open_index(settings: Settings, *, request_timeout: int = 5) -> SearchIndex:
    es = Elasticsearch(
        settings.elasticsearch_url,
        request_timeout=request_timeout,
        max_retries=0,
    )
    return SearchIndex(es, settings.elasticsearch_index, settings.embed_dims)


def _selected_names(settings: Settings, names: Sequence[str] | None) -> list[str]:
    specs = {
        "jira": settings.jira,
        "confluence": settings.confluence,
        "chat": settings.chat,
    }
    if names is None:
        return [name for name in SOURCES if specs[name].enabled]
    unknown = [name for name in names if name not in specs]
    if unknown:
        raise IngestConfigError(f"Unknown connector {unknown[0]}")
    return list(names)


async def with_worker(
    settings: Settings,
    fn: Callable[[IngestWorker], Awaitable[object]],
    *,
    names: Sequence[str] | None = None,
) -> object:
    watching = WatchStore(settings.watch_path).resolved(settings)
    settings = overlay_settings(settings, watching)
    selected = _selected_names(settings, names)
    specs = {
        "jira": settings.jira,
        "confluence": settings.confluence,
        "chat": settings.chat,
    }
    needed = {specs[name].mcp for name in selected if specs[name].enabled}
    async with AsyncExitStack() as stack:
        callers = {}
        for spec in settings.foundation_mcps:
            if spec.name not in needed:
                continue
            callers[spec.name] = await stack.enter_async_context(FoundationClient(spec))
        missing = needed - set(callers)
        if missing:
            raise IngestConfigError(
                f"Enabled sources need Foundation MCPs {sorted(missing)} in ragtone.yaml"
            )
        store = open_index(settings)
        store.ensure_index()
        checkpoints = CheckpointStore(settings.checkpoint_path)
        connectors = [
            connector
            for connector in build_connectors(settings, callers, checkpoints)
            if names is None or connector.name in selected
        ]
        worker = IngestWorker(
            store,
            build_embedder(
                settings.embedder,
                settings.embed_model,
                settings.embed_dims,
                url=settings.embed_url,
                token=settings.embed_token,
            ),
            checkpoints,
            connectors,
            poll_seconds=settings.poll_seconds,
        )
        return await fn(worker)
