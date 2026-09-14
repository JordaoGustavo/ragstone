from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from ragtone.ingest.oauth import build_oauth_provider
from ragtone.settings import FoundationMcp


class ToolCaller(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...

    async def list_tools(self) -> list[dict[str, str]]: ...


def parse_mcp_result(result: Any) -> Any:
    texts: list[str] = []
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    raw = "\n".join(texts).strip()
    if not raw:
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            return structured
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


class FoundationClient:
    def __init__(self, spec: FoundationMcp, data_dir: Path) -> None:
        self.spec = spec
        self.data_dir = data_dir
        self._client: Client | None = None

    def _connect(self) -> Client:
        if self.spec.transport == "stdio":
            if not self.spec.command:
                raise ValueError(f"{self.spec.name} stdio MCP needs a command")
            return Client(
                StdioServerParameters(
                    command=self.spec.command,
                    args=self.spec.args,
                    env=self.spec.env or None,
                )
            )
        if self.spec.transport == "http":
            if not self.spec.url:
                raise ValueError(f"{self.spec.name} http MCP needs a url")
            # Auth only kicks in if the gateway answers 401; harmless for MCPs that don't need it.
            http_client = httpx2.AsyncClient(
                headers=self.spec.headers,
                auth=build_oauth_provider(self.spec, self.data_dir),
            )
            return Client(streamable_http_client(self.spec.url, http_client=http_client))
        raise ValueError(f"Unsupported transport {self.spec.transport}")

    async def __aenter__(self) -> FoundationClient:
        self._client = self._connect()
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.__aexit__(exc_type, exc, tb)

    async def list_tools(self) -> list[dict[str, str]]:
        assert self._client is not None
        listed = await self._client.list_tools()
        return [
            {
                "name": tool.name,
                "description": (tool.description or "").strip(),
            }
            for tool in listed.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        assert self._client is not None
        result = await self._client.call_tool(name, arguments)
        return parse_mcp_result(result)
