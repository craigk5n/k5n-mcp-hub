from __future__ import annotations

import asyncio
import base64
import json
import socket
from typing import Any, AsyncIterator

import aiohttp
import aiohttp.web
import httpx
import pytest
from fastapi import FastAPI

from mcp_hub.app import create_app
from mcp_hub.config import Settings, StorageConfig, AuthConfig, BasicAuthConfig
from mcp_hub.mcp.constants import (
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    STATELESS_PROTOCOL_VERSION,
)


@pytest.fixture(autouse=True)
def _isolate_storage_from_local_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let the test suite touch a developer's real registry file.

    ``create_app()`` with no explicit ``Settings`` calls ``load_settings()``, which reads
    ``config.yaml`` from the CWD. If a developer has set ``storage.type: json`` there (a common
    local convenience), every test that calls ``create_app()`` and registers a server would
    persist test data into the real ``mcp_servers.json`` — last-writer-wins, silently wiping
    their registry. Force in-memory storage for all tests via the highest-priority override.

    Tests that specifically exercise JSON storage pass explicit ``Settings``/``tmp_path`` to
    ``create_app()``, which bypasses ``load_settings()`` and is therefore unaffected.
    """
    monkeypatch.setenv("MCPHUB_STORAGE__TYPE", "inmemory")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        storage=StorageConfig(type="inmemory"),
        auth=AuthConfig(
            type="basic",
            basic_auth=BasicAuthConfig(register_user="admin", register_pass="admin123"),
        ),
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    app_instance = create_app(settings)
    yield app_instance
    context = getattr(app_instance.state, "context", None)
    if context is not None:
        for task in context.background_tasks:
            if not task.done():
                task.cancel()
        if context.background_tasks:
            await asyncio.wait_for(
                asyncio.gather(*context.background_tasks, return_exceptions=True),
                timeout=5.0,
            )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client_instance:
        yield client_instance


@pytest.fixture
def auth_header() -> dict[str, str]:
    credentials = base64.b64encode(b"admin:admin123").decode()
    return {"Authorization": f"Basic {credentials}"}


class FakeMCPServer:
    """Minimal MCP server for tests.

    ``protocol_version`` selects the revision it speaks. Handshake revisions
    (the default) answer ``initialize``; the stateless ``2026-07-28`` revision
    rejects ``initialize`` and implements ``server/discover`` instead, and its
    list results carry the required ``resultType``/``ttlMs``/``cacheScope``.
    """

    def __init__(self, protocol_version: str = PROTOCOL_VERSION) -> None:
        self.protocol_version = protocol_version
        self.handler_called: bool = False
        self.handler_call_count: int = 0
        # Capabilities served by the list endpoints; tests mutate these directly.
        self.tools: list[dict[str, Any]] = []
        self.prompts: list[dict[str, Any]] = []
        self.resources: list[dict[str, Any]] = []
        # Last JSON-RPC request seen, for asserting on wire shape (_meta, headers).
        self.last_request_body: dict[str, Any] | None = None
        self.last_request_headers: dict[str, str] | None = None
        self._app: aiohttp.web.Application | None = None
        self._runner: aiohttp.web.AppRunner | None = None
        self._site: aiohttp.web.TCPSite | None = None
        self._port: int = 0
        self._base_url: str = ""

    async def start(self) -> str:
        self._app = aiohttp.web.Application()
        self._app.router.add_post("/", self._handle_jsonrpc)
        self._app.router.add_get("/health", self._handle_health)

        self._runner = aiohttp.web.AppRunner(self._app)
        await self._runner.setup()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self._port = sock.getsockname()[1]
        sock.close()

        self._site = aiohttp.web.TCPSite(self._runner, "127.0.0.1", self._port)
        await self._site.start()

        self._base_url = f"http://127.0.0.1:{self._port}"
        return self._base_url

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _handle_jsonrpc(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        self.handler_called = True
        self.handler_call_count += 1

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return aiohttp.web.json_response(
                {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}},
                status=400,
            )

        self.last_request_body = body
        self.last_request_headers = dict(request.headers)

        request_id = body.get("id")
        method = body.get("method")

        response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        stateless = self.protocol_version == STATELESS_PROTOCOL_VERSION

        served = {"tools": self.tools, "prompts": self.prompts, "resources": self.resources}

        def list_result(key: str) -> dict[str, Any]:
            result: dict[str, Any] = {key: served[key]}
            if stateless:
                result.update({"resultType": "complete", "ttlMs": 60000, "cacheScope": "private"})
            return result

        if method == "initialize" and not stateless:
            response["result"] = {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "serverInfo": {"name": "fake-mcp-server", "version": "0.1.0"},
            }
        elif method == "server/discover" and stateless:
            response["result"] = {
                "protocolVersions": [self.protocol_version],
                "capabilities": {},
                "serverInfo": {"name": "fake-mcp-server", "version": "0.1.0"},
            }
        elif method == "tools/list":
            response["result"] = list_result("tools")
        elif method == "resources/list":
            response["result"] = list_result("resources")
        elif method == "prompts/list":
            response["result"] = list_result("prompts")
        elif method == "tools/call":
            result: dict[str, Any] = {"content": [{"type": "text", "text": "ok"}]}
            if stateless:
                result["resultType"] = "complete"
            response["result"] = result
        elif method == "notifications/initialized" and not stateless:
            return aiohttp.web.Response(status=202)
        else:
            response["error"] = {"code": METHOD_NOT_FOUND, "message": "Method not found"}

        return aiohttp.web.json_response(response)

    async def _handle_health(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response({"status": "ok", "uptime_seconds": 10})

    @property
    def base_url(self) -> str:
        return self._base_url


@pytest.fixture
async def fake_mcp_server() -> AsyncIterator[FakeMCPServer]:
    server = FakeMCPServer()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def fake_stateless_mcp_server() -> AsyncIterator[FakeMCPServer]:
    server = FakeMCPServer(protocol_version=STATELESS_PROTOCOL_VERSION)
    await server.start()
    yield server
    await server.stop()
