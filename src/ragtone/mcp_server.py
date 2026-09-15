from __future__ import annotations

import json
import logging

from mcp.server.mcpserver import MCPServer

from ragtone.retrieval import RetrievalService

log = logging.getLogger(__name__)


def build_mcp(retrieval: RetrievalService) -> MCPServer:
    mcp = MCPServer("ragtone")

    @mcp.tool()
    def search(
        query: str,
        source: str | None = None,
        channel: str | None = None,
        since: str | None = None,
        k: int = 8,
    ) -> str:
        """Semantic search over the local Jira, Confluence, and chat index."""
        hits = retrieval.search(
            query, source=source, channel=channel, since=since, k=k
        )
        return json.dumps(hits, ensure_ascii=False, indent=2)

    @mcp.tool()
    def thread(thread_id: str) -> str:
        """Return every indexed message in a chat thread."""
        return json.dumps(retrieval.thread(thread_id), ensure_ascii=False, indent=2)

    @mcp.tool()
    def issue(key: str) -> str:
        """Return the indexed Jira issue and its comments."""
        return json.dumps(retrieval.issue(key), ensure_ascii=False, indent=2)

    @mcp.tool()
    def page(page_id: str) -> str:
        """Return indexed Confluence sections for a page."""
        return json.dumps(retrieval.page(page_id), ensure_ascii=False, indent=2)

    return mcp


def run_mcp(mcp: MCPServer, host: str, port: int) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("MCP2 must bind localhost only")
    log.info("MCP2 listening on http://%s:%s/mcp", host, port)
    mcp.run(transport="streamable-http", host=host, port=port)
