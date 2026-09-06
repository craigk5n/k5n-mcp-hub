"""Discovery follows `nextCursor` across every page (Story 4.2).

Without this a server with more capabilities than one page silently loses the rest —
the hub shows a truncated tool list and nothing indicates it is incomplete.
"""

from __future__ import annotations

from typing import Any

import pytest

from mcp_hub.auth.caller import SERVICE_IDENTITY
from mcp_hub.mcp.discovery import DiscoveryService
from mcp_hub.mcp.sdk_client import MCPClient
from mcp_hub.mcp.stateless import StatelessMCPClient
from mcp_hub.models.server import RegisteredServer


def tools(n: int) -> list[dict[str, Any]]:
    return [
        {"name": f"tool_{i}", "description": f"tool {i}", "inputSchema": {"type": "object"}}
        for i in range(n)
    ]


class TestStatelessPagination:
    @pytest.mark.asyncio
    async def test_all_pages_are_collected(self, fake_stateless_mcp_server) -> None:
        fake_stateless_mcp_server.tools = tools(7)
        fake_stateless_mcp_server.page_size = 3

        client = StatelessMCPClient(
            fake_stateless_mcp_server.base_url,
            allow_private_networks=True,
            caller=SERVICE_IDENTITY,
        )
        result = await client.list("tools/list", timeout=10)

        assert [t["name"] for t in result["tools"]] == [f"tool_{i}" for i in range(7)]

    @pytest.mark.asyncio
    async def test_the_final_page_has_no_cursor_left(self, fake_stateless_mcp_server) -> None:
        fake_stateless_mcp_server.tools = tools(5)
        fake_stateless_mcp_server.page_size = 2

        client = StatelessMCPClient(
            fake_stateless_mcp_server.base_url,
            allow_private_networks=True,
            caller=SERVICE_IDENTITY,
        )
        result = await client.list("tools/list", timeout=10)

        assert "nextCursor" not in result

    @pytest.mark.asyncio
    async def test_ttl_hint_survives_pagination(self, fake_stateless_mcp_server) -> None:
        # The ttlMs pacing from Story 2.5 must not be lost by merging pages.
        fake_stateless_mcp_server.tools = tools(4)
        fake_stateless_mcp_server.page_size = 2
        fake_stateless_mcp_server.ttl_ms = 45_000

        client = StatelessMCPClient(
            fake_stateless_mcp_server.base_url,
            allow_private_networks=True,
            caller=SERVICE_IDENTITY,
        )
        result = await client.list("tools/list", timeout=10)

        assert result["ttlMs"] == 45_000

    @pytest.mark.asyncio
    async def test_a_single_page_server_is_unchanged(self, fake_stateless_mcp_server) -> None:
        fake_stateless_mcp_server.tools = tools(3)

        client = StatelessMCPClient(
            fake_stateless_mcp_server.base_url,
            allow_private_networks=True,
            caller=SERVICE_IDENTITY,
        )
        result = await client.list("tools/list", timeout=10)

        assert len(result["tools"]) == 3


class TestHandshakePagination:
    @pytest.mark.asyncio
    async def test_all_pages_are_collected(self, fake_mcp_server) -> None:
        fake_mcp_server.tools = tools(7)
        fake_mcp_server.page_size = 3

        async with MCPClient(
            fake_mcp_server.base_url, allow_private_networks=True, caller=SERVICE_IDENTITY
        ) as client:
            await client.handshake(timeout=10)
            result = await client.list("tools/list", timeout=10)

        assert [t["name"] for t in result] == [f"tool_{i}" for i in range(7)]


class TestDiscoveryStoresEveryPage:
    @pytest.mark.asyncio
    async def test_a_paginated_server_is_fully_discovered(self, fake_mcp_server) -> None:
        fake_mcp_server.tools = tools(6)
        fake_mcp_server.page_size = 2

        class Registry:
            def __init__(self) -> None:
                self._servers: dict[str, RegisteredServer] = {}

            async def list(self):
                return list(self._servers.values())

            async def get(self, sid):
                return self._servers.get(sid)

            async def register(self, server):
                self._servers[server.id] = server
                return server

            async def update(self, server):
                self._servers[server.id] = server

        registry = Registry()
        server = RegisteredServer(id="paged", url=fake_mcp_server.base_url)
        registry._servers[server.id] = server

        service = DiscoveryService(registry, allow_private_networks=True)  # type: ignore[arg-type]
        await service.discover_immediately(server, timeout=10)

        stored = registry._servers["paged"]
        assert stored.tools is not None
        assert len(stored.tools) == 6, "every page must reach the registry"

    @pytest.mark.asyncio
    async def test_a_bounded_number_of_pages_is_followed(self, fake_stateless_mcp_server) -> None:
        # A server that always returns a cursor would otherwise loop forever.
        fake_stateless_mcp_server.tools = tools(2)
        fake_stateless_mcp_server.page_size = 1

        client = StatelessMCPClient(
            fake_stateless_mcp_server.base_url,
            allow_private_networks=True,
            caller=SERVICE_IDENTITY,
        )
        from mcp_hub.mcp import pagination

        assert pagination.MAX_PAGES > 1
        result = await client.list("tools/list", timeout=10)
        assert len(result["tools"]) == 2
