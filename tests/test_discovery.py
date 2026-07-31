import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any

from mcp_hub.mcp.constants import (
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    STATELESS_PROTOCOL_VERSION,
)
from mcp_hub.mcp.discovery import DiscoveryService, extract_list_payload
from mcp_hub.mcp.sdk_client import InitializeResult, MCPClientError
from mcp_hub.mcp.stateless import DiscoverResult
from mcp_hub.models.server import RegisteredServer
from mcp_hub.registry.service import Registry

from tests.conftest import FakeMCPServer


def make_server(
    id: str = "test-id",
    url: str = "https://test.example.com/mcp",
    # Recorded handshake version → discovery goes straight to the legacy SDK
    # path (no stateless probe), which is what the pre-2026 tests exercise.
    mcp_protocol_version: str = PROTOCOL_VERSION,
) -> RegisteredServer:
    return RegisteredServer(
        id=id,
        url=url,
        name="Test Server",
        mcp_protocol_version=mcp_protocol_version,
    )


class MockStatelessClient:
    """Stand-in for StatelessMCPClient in unit tests (no network)."""

    instantiated = 0

    def __init__(
        self,
        base_url: str,
        *,
        server: RegisteredServer | None = None,
        allow_private_networks: bool = False,
    ) -> None:
        type(self).instantiated += 1
        self.base_url = base_url
        self.server = server

    async def discover(self, timeout: float = 30.0) -> DiscoverResult:
        raise MCPClientError("Method not found", kind="discover", jsonrpc_code=METHOD_NOT_FOUND)

    async def list(self, method: str, timeout: float = 30.0) -> Any:
        raise AssertionError("list() must not be called after a failed discover()")


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

    async def handshake(self, timeout: float = 30.0) -> InitializeResult:
        self._initialize_result = InitializeResult(
            server_name="test-server",
            server_version="1.0.0",
            protocol_version="2025-11-25",
            session_id="test-session",
            transport="http",
        )
        return self._initialize_result

    async def list(self, method: str, timeout: float = 30.0) -> Any:
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
            def __init__(
                self,
                base_url: str,
                *,
                server: RegisteredServer | None = None,
                allow_private_networks: bool = False,
            ) -> None:
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
            def __init__(
                self,
                base_url: str,
                *,
                server: RegisteredServer | None = None,
                allow_private_networks: bool = False,
            ) -> None:
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
            def __init__(
                self,
                base_url: str,
                *,
                server: RegisteredServer | None = None,
                allow_private_networks: bool = False,
            ) -> None:
                super().__init__(base_url, server=server)

            async def list(self, method: str, timeout: float = 30.0) -> Any:
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
            def __init__(
                self,
                base_url: str,
                *,
                server: RegisteredServer | None = None,
                allow_private_networks: bool = False,
            ) -> None:
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
            def __init__(
                self,
                base_url: str,
                *,
                server: RegisteredServer | None = None,
                allow_private_networks: bool = False,
            ) -> None:
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


class TestStatelessDiscovery:
    @pytest.mark.asyncio
    async def test_discovers_stateless_server_end_to_end(
        self, fake_stateless_mcp_server: FakeMCPServer
    ) -> None:
        fake_stateless_mcp_server.tools = [
            {"name": "echo", "inputSchema": {"type": "object", "properties": {}}}
        ]
        registry = MockRegistry()
        service = DiscoveryService(registry, allow_private_networks=True)  # type: ignore[arg-type]
        server = make_server(
            id="stateless-1",
            url=fake_stateless_mcp_server.base_url,
            mcp_protocol_version="",
        )

        await service.discover_immediately(server, timeout=5.0)

        assert server.mcp_protocol_version == STATELESS_PROTOCOL_VERSION
        assert server.mcp_conformant is True
        assert server.tools == fake_stateless_mcp_server.tools
        assert server.schema_conformant is True
        assert server.last_capability_sync is not None
        assert registry._registered, "discovery must persist the server"

    @pytest.mark.asyncio
    async def test_falls_back_to_initialize_on_method_not_found(self) -> None:
        tools_response = [{"name": "t", "inputSchema": {"type": "object", "properties": {}}}]

        class LegacyClient(MockMCPClient):
            def __init__(
                self,
                base_url: str,
                *,
                server: RegisteredServer | None = None,
                allow_private_networks: bool = False,
            ) -> None:
                super().__init__(
                    base_url, server=server, tools=tools_response, prompts=[], resources=[]
                )

        registry = MockRegistry()
        service = DiscoveryService(registry)  # type: ignore[arg-type]
        server = make_server(id="unknown-1", mcp_protocol_version="")

        with (
            patch("mcp_hub.mcp.discovery.StatelessMCPClient", MockStatelessClient),
            patch("mcp_hub.mcp.discovery.MCPClient", LegacyClient),
        ):
            before = MockStatelessClient.instantiated
            await service.discover_immediately(server)
            assert MockStatelessClient.instantiated == before + 1, "probe must run first"

        assert server.mcp_protocol_version == "2025-11-25"
        assert server.mcp_conformant is True
        assert server.tools == tools_response

    @pytest.mark.asyncio
    async def test_probe_skipped_for_recorded_handshake_version(self) -> None:
        class LegacyClient(MockMCPClient):
            def __init__(
                self,
                base_url: str,
                *,
                server: RegisteredServer | None = None,
                allow_private_networks: bool = False,
            ) -> None:
                super().__init__(
                    base_url,
                    server=server,
                    tools=[{"name": "t", "inputSchema": {"type": "object", "properties": {}}}],
                    prompts=[],
                    resources=[],
                )

        registry = MockRegistry()
        service = DiscoveryService(registry)  # type: ignore[arg-type]
        server = make_server(id="legacy-1", mcp_protocol_version=PROTOCOL_VERSION)

        with (
            patch("mcp_hub.mcp.discovery.StatelessMCPClient", MockStatelessClient),
            patch("mcp_hub.mcp.discovery.MCPClient", LegacyClient),
        ):
            before = MockStatelessClient.instantiated
            await service.discover_immediately(server)
            assert MockStatelessClient.instantiated == before, "no probe for known legacy"

    @pytest.mark.asyncio
    async def test_stateless_recorded_server_skips_sdk_path(
        self, fake_stateless_mcp_server: FakeMCPServer
    ) -> None:
        fake_stateless_mcp_server.tools = [
            {"name": "echo", "inputSchema": {"type": "object", "properties": {}}}
        ]
        registry = MockRegistry()
        service = DiscoveryService(registry, allow_private_networks=True)  # type: ignore[arg-type]
        server = make_server(
            id="stateless-2",
            url=fake_stateless_mcp_server.base_url,
            mcp_protocol_version=STATELESS_PROTOCOL_VERSION,
        )

        class ExplodingSDKClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise AssertionError("SDK client must not be used for stateless servers")

        with patch("mcp_hub.mcp.discovery.MCPClient", ExplodingSDKClient):
            await service.discover_immediately(server, timeout=5.0)

        assert server.tools == fake_stateless_mcp_server.tools


class TestTtlPacing:
    """Story 2.5: stateless list results carry a ttlMs freshness hint — discovery
    must not re-poll a server before the hint expires. No hint (legacy servers)
    keeps the fixed-interval behavior; explicit discovery bypasses pacing."""

    def _tool(self) -> dict[str, Any]:
        return {"name": "echo", "inputSchema": {"type": "object", "properties": {}}}

    def _service_and_server(self, fake: FakeMCPServer) -> tuple[DiscoveryService, MockRegistry]:
        registry = MockRegistry()
        service = DiscoveryService(registry, allow_private_networks=True)  # type: ignore[arg-type]
        server = make_server(
            id="paced", url=fake.base_url, mcp_protocol_version=STATELESS_PROTOCOL_VERSION
        )
        registry._servers[server.id] = server
        return service, registry

    @pytest.mark.asyncio
    async def test_fresh_ttl_skips_next_poll(self, fake_stateless_mcp_server) -> None:
        fake_stateless_mcp_server.tools = [self._tool()]
        fake_stateless_mcp_server.ttl_ms = 60_000
        service, _ = self._service_and_server(fake_stateless_mcp_server)

        await service.poll_once()
        count_after_first = fake_stateless_mcp_server.handler_call_count
        assert count_after_first > 0

        await service.poll_once()
        assert fake_stateless_mcp_server.handler_call_count == count_after_first, (
            "server with an unexpired ttlMs must not be re-polled"
        )

    @pytest.mark.asyncio
    async def test_expired_ttl_repolls(self, fake_stateless_mcp_server) -> None:
        fake_stateless_mcp_server.tools = [self._tool()]
        fake_stateless_mcp_server.ttl_ms = 0
        service, _ = self._service_and_server(fake_stateless_mcp_server)

        await service.poll_once()
        count_after_first = fake_stateless_mcp_server.handler_call_count

        await service.poll_once()
        assert fake_stateless_mcp_server.handler_call_count > count_after_first

    @pytest.mark.asyncio
    async def test_no_ttl_keeps_fixed_interval(self, fake_mcp_server) -> None:
        fake_mcp_server.tools = [self._tool()]
        registry = MockRegistry()
        service = DiscoveryService(registry, allow_private_networks=True)  # type: ignore[arg-type]
        server = make_server(id="legacy-paced", url=fake_mcp_server.base_url)
        registry._servers[server.id] = server

        await service.poll_once()
        count_after_first = fake_mcp_server.handler_call_count
        assert count_after_first > 0

        await service.poll_once()
        assert fake_mcp_server.handler_call_count > count_after_first, (
            "servers without a ttl hint keep the fixed-interval polling"
        )

    @pytest.mark.asyncio
    async def test_direct_discovery_bypasses_pacing(self, fake_stateless_mcp_server) -> None:
        fake_stateless_mcp_server.tools = [self._tool()]
        fake_stateless_mcp_server.ttl_ms = 60_000
        service, registry = self._service_and_server(fake_stateless_mcp_server)

        await service.poll_once()
        count_after_first = fake_stateless_mcp_server.handler_call_count

        await service.discover_immediately(registry._servers["paced"], timeout=5.0)
        assert fake_stateless_mcp_server.handler_call_count > count_after_first, (
            "an explicit discovery request must not be blocked by the ttl hint"
        )
