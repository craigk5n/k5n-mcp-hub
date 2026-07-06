import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any

from mcp_hub.mcp.discovery import DiscoveryService, extract_list_payload
from mcp_hub.mcp.sdk_client import InitializeResult
from mcp_hub.models.server import RegisteredServer
from mcp_hub.registry.service import Registry


def make_server(
    id: str = "test-id",
    url: str = "https://test.example.com/mcp",
) -> RegisteredServer:
    return RegisteredServer(
        id=id,
        url=url,
        name="Test Server",
    )


class MockClientSession:
    def __init__(
        self,
        tools: list[dict[str, Any]] | None = None,
        prompts: list[dict[str, Any]] | None = None,
        resources: list[dict[str, Any]] | None = None,
        raise_error: str | None = None,
    ) -> None:
        self._tools = tools
        self._prompts = prompts
        self._resources = resources
        self._raise_error = raise_error

    async def __aenter__(self) -> "MockClientSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def initialize(self) -> MagicMock:
        result = MagicMock()
        result.serverInfo = MagicMock(name="test-server", version="1.0.0")
        result.protocolVersion = "2025-11-25"
        return result

    async def send_notification(self, notification: Any) -> None:
        pass

    async def list_tools(self) -> MagicMock:
        if self._raise_error == "tools":
            raise Exception("Method not found")
        return MagicMock(tools=self._tools or [])

    async def list_prompts(self) -> MagicMock:
        if self._raise_error == "prompts":
            raise Exception("Method not found")
        return MagicMock(prompts=self._prompts or [])

    async def list_resources(self) -> MagicMock:
        if self._raise_error == "resources":
            raise Exception("Method not found")
        return MagicMock(resources=self._resources or [])


class MockMCPClient:
    def __init__(
        self,
        base_url: str,
        *,
        server: RegisteredServer | None = None,
        tools: list[dict[str, Any]] | None = None,
        prompts: list[dict[str, Any]] | None = None,
        resources: list[dict[str, Any]] | None = None,
        raise_list_error: str | None = None,
    ) -> None:
        self.base_url = base_url
        self.server = server
        self._tools = tools
        self._prompts = prompts
        self._resources = resources
        self._raise_list_error = raise_list_error
        self._initialize_result: InitializeResult | None = None
        self._session = MockClientSession(
            tools=tools,
            prompts=prompts,
            resources=resources,
            raise_error=raise_list_error,
        )

    @property
    def initialize_result(self) -> InitializeResult | None:
        return self._initialize_result

    async def __aenter__(self) -> "MockMCPClient":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    async def handshake(self) -> InitializeResult:
        self._initialize_result = InitializeResult(
            server_name="test-server",
            server_version="1.0.0",
            protocol_version="2025-11-25",
            session_id="test-session",
            transport="http",
        )
        return self._initialize_result

    async def list(self, method: str) -> Any:
        if method == "tools/list":
            return {"tools": self._tools} if self._tools else []
        if method == "prompts/list":
            return {"prompts": self._prompts} if self._prompts else []
        if method == "resources/list":
            return {"resources": self._resources} if self._resources else []
        return {}


class MockRegistry:
    def __init__(self) -> None:
        self._servers: dict[str, RegisteredServer] = {}
        self._registered: list[RegisteredServer] = []

    async def register(self, server: RegisteredServer) -> RegisteredServer:
        self._registered.append(server)
        self._servers[server.id] = server
        return server

    async def list(self) -> list[RegisteredServer]:
        return list(self._servers.values())


class TestExtractListPayload:
    def test_list_input_returns_list(self) -> None:
        result = extract_list_payload([{"name": "a"}], "tools")
        assert result == [{"name": "a"}]

    def test_dict_with_kind_key_returns_list(self) -> None:
        result = extract_list_payload({"tools": [1, 2]}, "tools")
        assert result == [1, 2]

    def test_dict_with_result_containing_kind(self) -> None:
        result = extract_list_payload({"result": {"tools": [1]}}, "tools")
        assert result == [1]

    def test_error_response_returns_none(self) -> None:
        result = extract_list_payload(
            {"error": {"code": -32601, "message": "not supported"}}, "tools"
        )
        assert result is None

    def test_empty_dict_returns_none(self) -> None:
        result = extract_list_payload({}, "tools")
        assert result is None


class TestDiscoveryService:
    @pytest.mark.asyncio
    async def test_discover_immediately_with_valid_tools(self) -> None:
        tools_response = [{"name": "foo", "inputSchema": {"type": "object", "properties": {}}}]

        class TestMockMCPClient(MockMCPClient):
            def __init__(self, base_url: str, *, server: RegisteredServer | None = None) -> None:
                super().__init__(
                    base_url, server=server, tools=tools_response, prompts=[], resources=[]
                )

        registry = MockRegistry()
        service = DiscoveryService(registry)  # type: ignore[arg-type]

        server = make_server(id="server-1", url="https://test.example.com/mcp")

        with patch("mcp_hub.mcp.discovery.MCPClient", TestMockMCPClient):
            await service.discover_immediately(server)

        assert server.tools == tools_response
        assert server.schema_conformant is True
        assert server.schema_issues == []

    @pytest.mark.asyncio
    async def test_discover_immediately_raises_when_no_capabilities(self) -> None:
        class EmptyMockMCPClient(MockMCPClient):
            def __init__(self, base_url: str, *, server: RegisteredServer | None = None) -> None:
                super().__init__(base_url, server=server, tools=[], prompts=[], resources=[])

        registry = MockRegistry()
        service = DiscoveryService(registry)  # type: ignore[arg-type]

        server = make_server(id="server-2", url="https://test.example.com/mcp")

        with patch("mcp_hub.mcp.discovery.MCPClient", EmptyMockMCPClient):
            with pytest.raises(
                RuntimeError, match="discovery completed but no capabilities were found"
            ):
                await service.discover_immediately(server)

    @pytest.mark.asyncio
    async def test_discover_immediately_raises_when_json_rpc_error(self) -> None:
        class ErrorMockMCPClient(MockMCPClient):
            def __init__(self, base_url: str, *, server: RegisteredServer | None = None) -> None:
                super().__init__(base_url, server=server)

            async def list(self, method: str) -> Any:
                return {"error": {"code": -32601}}

        registry = MockRegistry()
        service = DiscoveryService(registry)  # type: ignore[arg-type]

        server = make_server(id="server-3", url="https://test.example.com/mcp")

        with patch("mcp_hub.mcp.discovery.MCPClient", ErrorMockMCPClient):
            with pytest.raises(
                RuntimeError, match="discovery completed but no capabilities were found"
            ):
                await service.discover_immediately(server)

    @pytest.mark.asyncio
    async def test_poll_once_does_not_raise_on_server_error(self) -> None:
        class FailingRegistry:
            def __init__(self) -> None:
                self._servers = {
                    "server-1": make_server(id="server-1", url="https://test1.example.com/mcp"),
                    "server-2": make_server(id="server-2", url="https://test2.example.com/mcp"),
                }

            async def register(self, server: RegisteredServer) -> RegisteredServer:
                return server

            async def list(self) -> list[RegisteredServer]:
                return list(self._servers.values())

        class FailingClient(MockMCPClient):
            def __init__(self, base_url: str, *, server: RegisteredServer | None = None) -> None:
                super().__init__(base_url, server=server)
                if "test1" in base_url:
                    self._raise_list_error = "tools"

        registry = FailingRegistry()
        service = DiscoveryService(registry)  # type: ignore[arg-type]

        with patch("mcp_hub.mcp.discovery.MCPClient", FailingClient):
            await service.poll_once()

    @pytest.mark.asyncio
    async def test_discover_immediately_sets_protocol_version(self) -> None:
        tools_response = [{"name": "foo", "inputSchema": {"type": "object", "properties": {}}}]

        class TestMockMCPClient(MockMCPClient):
            def __init__(self, base_url: str, *, server: RegisteredServer | None = None) -> None:
                super().__init__(
                    base_url, server=server, tools=tools_response, prompts=[], resources=[]
                )

        registry = MockRegistry()
        service = DiscoveryService(registry)  # type: ignore[arg-type]

        server = make_server(id="server-4", url="https://test.example.com/mcp")

        with patch("mcp_hub.mcp.discovery.MCPClient", TestMockMCPClient):
            await service.discover_immediately(server)

        assert server.mcp_protocol_version == "2025-11-25"
        assert server.mcp_conformant is True
        assert server.mcp_transport == "http"
        assert server.last_capability_sync is not None
