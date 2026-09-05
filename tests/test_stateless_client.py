"""Tests for the hand-rolled stateless (2026-07-28) MCP client.

The official ``mcp`` SDK doesn't speak the stateless revision yet, so
``StatelessMCPClient`` issues single JSON-RPC POSTs with the spec's ``_meta``
keys and no initialize handshake.
"""

from __future__ import annotations

import pytest

from mcp_hub.mcp.constants import (
    MCP_CLIENT_NAME,
    METHOD_NOT_FOUND,
    STATELESS_PROTOCOL_VERSION,
)
from mcp_hub.mcp.sdk_client import MCPClientError
from mcp_hub.mcp.stateless import StatelessMCPClient

from tests.conftest import FakeMCPServer
from mcp_hub.auth.caller import SERVICE_IDENTITY


class TestDiscover:
    @pytest.mark.asyncio
    async def test_discover_returns_advertised_version(
        self, fake_stateless_mcp_server: FakeMCPServer
    ) -> None:
        client = StatelessMCPClient(
            fake_stateless_mcp_server.base_url, allow_private_networks=True, caller=SERVICE_IDENTITY
        )
        result = await client.discover(timeout=5.0)
        assert result.protocol_version == STATELESS_PROTOCOL_VERSION
        assert result.server_name == "fake-mcp-server"

    @pytest.mark.asyncio
    async def test_discover_against_legacy_server_raises_method_not_found(
        self, fake_mcp_server: FakeMCPServer
    ) -> None:
        client = StatelessMCPClient(
            fake_mcp_server.base_url, allow_private_networks=True, caller=SERVICE_IDENTITY
        )
        with pytest.raises(MCPClientError) as exc_info:
            await client.discover(timeout=5.0)
        assert exc_info.value.jsonrpc_code == METHOD_NOT_FOUND
        assert exc_info.value.is_method_not_found

    @pytest.mark.asyncio
    async def test_discover_request_carries_meta_and_headers(
        self, fake_stateless_mcp_server: FakeMCPServer
    ) -> None:
        client = StatelessMCPClient(
            fake_stateless_mcp_server.base_url, allow_private_networks=True, caller=SERVICE_IDENTITY
        )
        await client.discover(timeout=5.0)

        body = fake_stateless_mcp_server.last_request_body
        assert body is not None
        meta = body["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == STATELESS_PROTOCOL_VERSION
        assert meta["io.modelcontextprotocol/clientCapabilities"] == {}
        assert meta["io.modelcontextprotocol/clientInfo"]["name"] == MCP_CLIENT_NAME

        headers = fake_stateless_mcp_server.last_request_headers
        assert headers is not None
        assert headers.get("MCP-Protocol-Version") == STATELESS_PROTOCOL_VERSION
        # 2026-07-28 requires Mcp-Method on Streamable HTTP POSTs.
        assert headers.get("Mcp-Method") == "server/discover"


class TestList:
    @pytest.mark.asyncio
    async def test_list_tools_returns_result_payload(
        self, fake_stateless_mcp_server: FakeMCPServer
    ) -> None:
        fake_stateless_mcp_server.tools = [
            {"name": "echo", "inputSchema": {"type": "object", "properties": {}}}
        ]
        client = StatelessMCPClient(
            fake_stateless_mcp_server.base_url, allow_private_networks=True, caller=SERVICE_IDENTITY
        )
        result = await client.list("tools/list", timeout=5.0)
        assert result["tools"] == fake_stateless_mcp_server.tools

    @pytest.mark.asyncio
    async def test_list_sends_mcp_method_header(
        self, fake_stateless_mcp_server: FakeMCPServer
    ) -> None:
        client = StatelessMCPClient(
            fake_stateless_mcp_server.base_url, allow_private_networks=True, caller=SERVICE_IDENTITY
        )
        await client.list("tools/list", timeout=5.0)
        headers = fake_stateless_mcp_server.last_request_headers
        assert headers is not None
        assert headers.get("Mcp-Method") == "tools/list"

    @pytest.mark.asyncio
    async def test_unreachable_server_raises_client_error(self) -> None:
        client = StatelessMCPClient(
            "http://127.0.0.1:9", allow_private_networks=True, caller=SERVICE_IDENTITY
        )
        with pytest.raises(MCPClientError) as exc_info:
            await client.discover(timeout=2.0)
        assert not exc_info.value.is_method_not_found
