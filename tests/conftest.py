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
    def __init__(self) -> None:
        self.handler_called: bool = False
        self.handler_call_count: int = 0
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

        request_id = body.get("id")
        method = body.get("method")

        response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}

        if method == "initialize":
            response["result"] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fake-mcp-server", "version": "0.1.0"},
            }
        elif method == "tools/list":
            response["result"] = {"tools": []}
        elif method == "resources/list":
            response["result"] = {"resources": []}
        elif method == "prompts/list":
            response["result"] = {"prompts": []}
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}

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
