from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from ragtone.settings import FoundationMcp

log = logging.getLogger(__name__)

# ponytail: fixed loopback port, simplest thing that works for a single-user local tool.
# Bump if it collides with another local service.
CALLBACK_PORT = 8767


class FileTokenStorage(TokenStorage):
    """Persists OAuth tokens/client registration to a JSON file, one per Foundation MCP."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text())

    def _save(self, data: dict) -> None:
        # Tokens are secrets: keep the file and its directory owner-only.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self._path.parent, 0o700)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data))

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._load().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._load()
        data["tokens"] = tokens.model_dump(mode="json")
        self._save(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._load()
        data["client_info"] = client_info.model_dump(mode="json")
        self._save(data)


def _callback_handler_class(
    result: list[AuthorizationCodeResult], event: threading.Event
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            params = parse_qs(urlparse(self.path).query)
            result.append(
                AuthorizationCodeResult(
                    code=params.get("code", [""])[0],
                    state=params.get("state", [None])[0],
                    iss=params.get("iss", [None])[0],
                )
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ragtone: authorized, you can close this tab.")
            event.set()

        def log_message(self, *args: object) -> None:
            pass

    return Handler


async def await_callback(port: int = CALLBACK_PORT) -> AuthorizationCodeResult:
    """Wait for the single OAuth redirect on the loopback callback server."""
    event = threading.Event()
    result: list[AuthorizationCodeResult] = []
    server = HTTPServer(("127.0.0.1", port), _callback_handler_class(result, event))
    threading.Thread(target=server.handle_request, daemon=True).start()
    await asyncio.to_thread(event.wait)
    server.server_close()
    return result[0]


async def open_browser(url: str) -> None:
    log.info("Open this URL to authorize: %s", url)
    webbrowser.open(url)


def build_oauth_provider(spec: FoundationMcp, data_dir: Path) -> OAuthClientProvider:
    assert spec.url
    return OAuthClientProvider(
        server_url=spec.url,
        client_metadata=OAuthClientMetadata(
            client_name="ragtone",
            redirect_uris=[f"http://127.0.0.1:{CALLBACK_PORT}/callback"],
            grant_types=["authorization_code", "refresh_token"],
        ),
        storage=FileTokenStorage(data_dir / "oauth" / f"{spec.name}.json"),
        redirect_handler=open_browser,
        callback_handler=await_callback,
    )
