import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any

from devhub.mcp.sdk_client import MCPClient, MCPClientError, InitializeResult
from devhub.models.server import RegisteredServer


def make_server(
    id: str = "test-id",
    url: str = "https://test.example.com",
    bearer_token: str = "",
) -> RegisteredServer:
    return RegisteredServer(
        id=id,
        url=url,
        bearer_token=bearer_token,
    )


class MockServerInfo:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version


class MockInitializeResult:
    def __init__(
        self,
        protocolVersion: str = "2025-11-25",
        serverInfo: MockServerInfo | None = None,
    ) -> None:
        self.protocolVersion = protocolVersion
        self.serverInfo = serverInfo or MockServerInfo("test-server", "1.0.0")
        self.capabilities: dict[str, Any] = {}


class MockClientSession:
    def __init__(self, **kwargs: Any) -> None:
        self._initialized = False

    async def __aenter__(self) -> "MockClientSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def initialize(self) -> MockInitializeResult:
        self._initialized = True
        return MockInitializeResult(
            protocolVersion="2025-11-25",
            serverInfo=MockServerInfo("test-server", "1.0.0"),
        )

    async def send_notification(self, notification: Any) -> None:
        pass

    async def list_tools(self) -> MagicMock:
        return MagicMock(tools=[{"name": "tool1", "description": "Test tool 1"}])

    async def list_prompts(self) -> MagicMock:
        return MagicMock(prompts=[{"name": "prompt1", "description": "Test prompt 1"}])

    async def list_resources(self) -> MagicMock:
        return MagicMock(resources=[{"uri": "resource://test", "name": "test"}])


@pytest.mark.asyncio
async def test_handshake_returns_initialize_result_with_protocol_version() -> None:
    mock_session = MockClientSession()

    with patch("devhub.mcp.sdk_client._get_streamable_http_client") as mock_get:
        mock_get.return_value = lambda url, **kwargs: mock_context_manager()

        with patch("mcp.client.session.ClientSession", return_value=mock_session):
            client = MCPClient("http://localhost:3000/mcp")

            with patch.object(
                client,
                "_open_transport",
                new_callable=AsyncMock,
            ):
                client._session = mock_session  # type: ignore[assignment]
                result = await client.handshake()

                assert isinstance(result, InitializeResult)
                assert result.protocol_version == "2025-11-25"
                assert result.server_name == "test-server"
                assert result.server_version == "1.0.0"


def mock_context_manager() -> Any:
    def get_session_id() -> str:
        return "test-session-id"

    async def enter(self: Any = None):
        read_stream = AsyncMock()
        write_stream = AsyncMock()
        return (read_stream, write_stream, get_session_id)

    async def exit(self: Any = None, *args: Any):
        pass

    mock_cm = MagicMock()
    mock_cm.__aenter__ = enter
    mock_cm.__aexit__ = exit
    return mock_cm


@pytest.mark.asyncio
async def test_list_tools_returns_list_of_tool_dicts() -> None:
    mock_session: Any = MockClientSession()

    client = MCPClient("http://localhost:3000/mcp")
    client._session = mock_session

    result = await client.list("tools/list")

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["name"] == "tool1"


@pytest.mark.asyncio
async def test_list_prompts_returns_list() -> None:
    mock_session = MockClientSession()

    client = MCPClient("http://localhost:3000/mcp")
    client._session = mock_session  # type: ignore[assignment]

    result = await client.list("prompts/list")

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["name"] == "prompt1"


@pytest.mark.asyncio
async def test_list_resources_returns_list() -> None:
    mock_session = MockClientSession()

    client = MCPClient("http://localhost:3000/mcp")
    client._session = mock_session  # type: ignore[assignment]

    result = await client.list("resources/list")

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["uri"] == "resource://test"


@pytest.mark.asyncio
async def test_list_without_handshake_raises_error() -> None:
    client = MCPClient("http://localhost:3000/mcp")

    with pytest.raises(MCPClientError) as exc_info:
        await client.list("tools/list")

    assert exc_info.value.kind == "list"


@pytest.mark.asyncio
async def test_list_invalid_method_raises_error() -> None:
    mock_session = MockClientSession()

    client = MCPClient("http://localhost:3000/mcp")
    client._session = mock_session  # type: ignore[assignment]

    with pytest.raises(MCPClientError) as exc_info:
        await client.list("invalid/method")

    assert exc_info.value.kind == "list"


@pytest.mark.asyncio
async def test_ping_unreachable_url_raises_mcp_client_error() -> None:
    client = MCPClient("http://localhost:19999/nonexistent")

    with pytest.raises(MCPClientError) as exc_info:
        await client.ping(timeout=1.0)

    assert exc_info.value.kind == "ping"


@pytest.mark.asyncio
async def test_mcp_client_error_kind() -> None:
    error = MCPClientError("test error", kind="handshake")
    assert error.kind == "handshake"

    error = MCPClientError("test error", kind="list")
    assert error.kind == "list"

    error = MCPClientError("test error", kind="ping")
    assert error.kind == "ping"

    error = MCPClientError("test error", kind="transport")
    assert error.kind == "transport"


@pytest.mark.asyncio
async def test_initialize_result_properties() -> None:
    result = InitializeResult(
        server_name="test-server",
        server_version="1.0.0",
        protocol_version="2025-11-25",
        session_id="session-123",
        transport="http",
    )

    assert result.server_name == "test-server"
    assert result.server_version == "1.0.0"
    assert result.protocol_version == "2025-11-25"
    assert result.session_id == "session-123"
    assert result.transport == "http"


@pytest.mark.asyncio
async def test_client_context_manager() -> None:
    mock_session = MockClientSession()

    with patch("devhub.mcp.sdk_client._get_streamable_http_client") as mock_get:
        mock_get.return_value = lambda url, **kwargs: mock_context_manager()

        with patch("mcp.client.session.ClientSession", return_value=mock_session):
            async with MCPClient("http://localhost:3000/mcp") as client:
                result = await client.handshake()
                assert isinstance(result, InitializeResult)
                assert result.protocol_version == "2025-11-25"


@pytest.mark.asyncio
async def test_bearer_token_in_headers() -> None:
    server = make_server(bearer_token="test-token-123")

    from devhub.mcp.auth import apply_server_auth

    test_headers: dict[str, str] = {}
    await apply_server_auth(test_headers, server)

    assert "Authorization" in test_headers
    assert test_headers["Authorization"] == "Bearer test-token-123"
