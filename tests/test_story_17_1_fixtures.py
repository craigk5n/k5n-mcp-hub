from __future__ import annotations

import base64

import httpx
import pytest

from mcp_hub.config import AuthConfig, BasicAuthConfig, Settings, StorageConfig

from tests.conftest import FakeMCPServer


class TestFixtures:
    def test_settings_has_expected_storage_config(self, settings: Settings) -> None:
        assert settings.storage.type == "inmemory"

    def test_settings_has_expected_auth_config(self, settings: Settings) -> None:
        assert settings.auth.type == "basic"
        assert settings.auth.basic_auth.register_user == "admin"
        assert settings.auth.basic_auth.register_pass == "admin123"

    @pytest.mark.asyncio
    async def test_client_can_issue_request(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/healthz")
        assert response.status_code == 200

    def test_auth_header_decodes_to_admin_admin123(self, auth_header: dict[str, str]) -> None:
        assert "Authorization" in auth_header
        assert auth_header["Authorization"].startswith("Basic ")
        credentials = auth_header["Authorization"].split(" ", 1)[1]
        decoded = base64.b64decode(credentials).decode()
        assert decoded == "admin:admin123"

    @pytest.mark.asyncio
    async def test_fake_mcp_server_starts_on_random_port(
        self, fake_mcp_server: FakeMCPServer
    ) -> None:
        assert fake_mcp_server.base_url.startswith("http://127.0.0.1:")
        port_str = fake_mcp_server.base_url.split(":")[-1]
        port = int(port_str)
        assert 1 <= port <= 65535

    @pytest.mark.asyncio
    async def test_fake_mcp_server_responds_to_health(self, fake_mcp_server: FakeMCPServer) -> None:
        import aiohttp
        import aiohttp.web

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{fake_mcp_server.base_url}/health") as response:
                assert response.status == 200
                body = await response.json()
                assert body == {"status": "ok", "uptime_seconds": 10}

    @pytest.mark.asyncio
    async def test_fake_mcp_server_responds_to_initialize(
        self, fake_mcp_server: FakeMCPServer
    ) -> None:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            request_body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            }
            async with session.post(f"{fake_mcp_server.base_url}/", json=request_body) as response:
                assert response.status == 200
                body = await response.json()
                assert body["jsonrpc"] == "2.0"
                assert body["id"] == 1
                assert "result" in body
                assert body["result"]["serverInfo"]["name"] == "fake-mcp-server"

    @pytest.mark.asyncio
    async def test_fake_mcp_server_records_handler_invocation(
        self, fake_mcp_server: FakeMCPServer
    ) -> None:
        import aiohttp

        assert fake_mcp_server.handler_called is False
        assert fake_mcp_server.handler_call_count == 0

        async with aiohttp.ClientSession() as session:
            request_body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            }
            async with session.post(f"{fake_mcp_server.base_url}/", json=request_body) as response:
                await response.json()

        assert fake_mcp_server.handler_called is True
        assert fake_mcp_server.handler_call_count == 1

    @pytest.mark.asyncio
    async def test_fake_mcp_server_tears_down_cleanly(self, fake_mcp_server: FakeMCPServer) -> None:
        import aiohttp

        base_url = fake_mcp_server.base_url

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/health") as response:
                assert response.status == 200

        await fake_mcp_server.stop()

        with pytest.raises(aiohttp.ClientConnectorError):
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base_url}/health") as response:
                    pass
