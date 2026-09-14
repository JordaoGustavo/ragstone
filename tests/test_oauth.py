from __future__ import annotations

import asyncio
import urllib.request
from pathlib import Path

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from ragtone.ingest.oauth import FileTokenStorage, await_callback


def test_file_token_storage_round_trip(tmp_path: Path) -> None:
    storage = FileTokenStorage(tmp_path / "vela.json")

    assert asyncio.run(storage.get_tokens()) is None
    assert asyncio.run(storage.get_client_info()) is None

    asyncio.run(storage.set_tokens(OAuthToken(access_token="abc", refresh_token="r1")))
    asyncio.run(
        storage.set_client_info(OAuthClientInformationFull(client_id="ragtone-1"))
    )

    tokens = asyncio.run(storage.get_tokens())
    assert tokens is not None
    assert tokens.access_token == "abc"
    assert tokens.refresh_token == "r1"

    client_info = asyncio.run(storage.get_client_info())
    assert client_info is not None
    assert client_info.client_id == "ragtone-1"


def test_await_callback_parses_redirect_query() -> None:
    async def scenario() -> None:
        port = 8768  # distinct from CALLBACK_PORT to avoid clashing with a live run
        task = asyncio.create_task(await_callback(port))
        await asyncio.sleep(0.1)  # let the server bind before we hit it
        await asyncio.to_thread(
            urllib.request.urlopen,
            f"http://127.0.0.1:{port}/callback?code=xyz&state=s1",
        )
        result = await task
        assert result.code == "xyz"
        assert result.state == "s1"

    asyncio.run(scenario())
