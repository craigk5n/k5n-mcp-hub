import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any

from mcp_hub.mcp.sdk_client import MCPClient, MCPClientError, InitializeResult
from mcp_hub.models.server import RegisteredServer
from mcp_hub.auth.caller import SERVICE_IDENTITY


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

    async def initialize(self) -> Any:
        self._initialized = True
        from mcp.types import Implementation, InitializeResult as SDKInitializeResult
        from mcp.types import ServerCapabilities

        return SDKInitializeResult(
            protocol_version="2025-11-25",
            capabilities=ServerCapabilities(),
            server_info=Implementation(name="test-server", version="1.0.0"),
        )

    async def send_notification(self, notification: Any) -> None:
        pass

    # `params` mirrors the real ClientSession signature, which takes
    # PaginatedRequestParams. Without it the mock silently diverges from the SDK.
    async def list_tools(self, params: Any = None) -> MagicMock:
        return MagicMock(tools=[{"name": "tool1", "description": "Test tool 1"}], next_cursor=None)

    async def list_prompts(self, params: Any = None) -> MagicMock:
        return MagicMock(
            prompts=[{"name": "prompt1", "description": "Test prompt 1"}], next_cursor=None
        )

    async def list_resources(self, params: Any = None) -> MagicMock:
        return MagicMock(resources=[{"uri": "resource://test", "name": "test"}])


@pytest.mark.asyncio
async def test_handshake_returns_initialize_result_with_protocol_version() -> None:
    mock_session = MockClientSession()

    with patch("mcp_hub.mcp.sdk_client._get_streamable_http_client") as mock_get:
        mock_get.return_value = lambda url, **kwargs: mock_context_manager()

        with patch("mcp.client.session.ClientSession", return_value=mock_session):
            client = MCPClient("http://localhost:3000/mcp", caller=SERVICE_IDENTITY)

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
        # `mcp` 2.x yields two values; the session id now comes from a response hook.
        return (read_stream, write_stream)

    async def exit(self: Any = None, *args: Any):
        pass

    mock_cm = MagicMock()
    mock_cm.__aenter__ = enter
    mock_cm.__aexit__ = exit
    return mock_cm


@pytest.mark.asyncio
async def test_list_tools_returns_list_of_tool_dicts() -> None:
    mock_session: Any = MockClientSession()

    client = MCPClient("http://localhost:3000/mcp", caller=SERVICE_IDENTITY)
    client._session = mock_session

    result = await client.list("tools/list")

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["name"] == "tool1"


@pytest.mark.asyncio
async def test_list_prompts_returns_list() -> None:
    mock_session = MockClientSession()

    client = MCPClient("http://localhost:3000/mcp", caller=SERVICE_IDENTITY)
    client._session = mock_session  # type: ignore[assignment]

    result = await client.list("prompts/list")

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["name"] == "prompt1"


@pytest.mark.asyncio
async def test_list_resources_returns_list() -> None:
    mock_session = MockClientSession()

    client = MCPClient("http://localhost:3000/mcp", caller=SERVICE_IDENTITY)
    client._session = mock_session  # type: ignore[assignment]

    result = await client.list("resources/list")

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["uri"] == "resource://test"


@pytest.mark.asyncio
async def test_list_without_handshake_raises_error() -> None:
    client = MCPClient("http://localhost:3000/mcp", caller=SERVICE_IDENTITY)

    with pytest.raises(MCPClientError) as exc_info:
        await client.list("tools/list")

    assert exc_info.value.kind == "list"


@pytest.mark.asyncio
async def test_list_invalid_method_raises_error() -> None:
    mock_session = MockClientSession()

    client = MCPClient("http://localhost:3000/mcp", caller=SERVICE_IDENTITY)
    client._session = mock_session  # type: ignore[assignment]

    with pytest.raises(MCPClientError) as exc_info:
        await client.list("invalid/method")

    assert exc_info.value.kind == "list"


@pytest.mark.asyncio
async def test_ping_unreachable_url_raises_mcp_client_error() -> None:
    client = MCPClient("http://localhost:19999/nonexistent", caller=SERVICE_IDENTITY)

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

    with patch("mcp_hub.mcp.sdk_client._get_streamable_http_client") as mock_get:
        mock_get.return_value = lambda url, **kwargs: mock_context_manager()

        with patch("mcp.client.session.ClientSession", return_value=mock_session):
            async with MCPClient("http://localhost:3000/mcp", caller=SERVICE_IDENTITY) as client:
                result = await client.handshake()
                assert isinstance(result, InitializeResult)
                assert result.protocol_version == "2025-11-25"


@pytest.mark.asyncio
async def test_bearer_token_in_headers() -> None:
    server = make_server(bearer_token="test-token-123")

    from mcp_hub.mcp.auth import apply_server_auth

    test_headers: dict[str, str] = {}
    await apply_server_auth(test_headers, server, caller=SERVICE_IDENTITY)

    assert "Authorization" in test_headers
    assert test_headers["Authorization"] == "Bearer test-token-123"


class TestSDKTwoMigration:
    """Story 4.1: pins the `mcp` 2.x API surface this client depends on.

    Each of these was a silent breakage during the 1.x -> 2.x migration: the tests
    passed against hand-rolled mocks that answered to whatever attribute was asked
    for, so only a real server would have surfaced them.
    """

    def test_the_transport_factory_resolves(self) -> None:
        from mcp_hub.mcp.sdk_client import _get_streamable_http_client

        assert callable(_get_streamable_http_client())

    def test_initialize_result_uses_snake_case_fields(self) -> None:
        # 2.x renamed serverInfo/protocolVersion. Reading the SDK's own model rather
        # than asserting our strings means a future rename fails here.
        from mcp.types import InitializeResult as SDKInitializeResult

        fields = set(SDKInitializeResult.model_fields)
        assert {"protocol_version", "server_info"} <= fields
        assert "protocolVersion" not in fields
        assert "serverInfo" not in fields

    def test_initialized_notification_is_sent_unwrapped(self) -> None:
        # 2.x turned ClientNotification into a union type, so the old
        # ClientNotification(root=...) wrapper raises TypeError at runtime.
        import mcp.types as types

        assert hasattr(types, "InitializedNotification")
        with pytest.raises(TypeError):
            types.ClientNotification(root=types.InitializedNotification())  # type: ignore[operator]

    @pytest.mark.asyncio
    async def test_handshake_records_the_streamable_transport_marker(self) -> None:
        # "sse" is this codebase's name for streamable HTTP (see _health_badge.html and
        # ui_downloads.py's is_streamable). Recording "http" would mean "plain
        # non-streaming JSON", mislabel the badge, and flip the generated script to the
        # wrong variant.
        mock_session = MockClientSession()

        with patch("mcp_hub.mcp.sdk_client._get_streamable_http_client") as mock_get:
            mock_get.return_value = lambda url, **kwargs: mock_context_manager()
            client = MCPClient("http://localhost:3000/mcp", caller=SERVICE_IDENTITY)
            with patch.object(client, "_open_transport", new_callable=AsyncMock):
                client._session = mock_session  # type: ignore[assignment]
                result = await client.handshake()

        assert result.transport == "sse"


class TestTolerantListParsing:
    """A server that gets one tool's schema wrong must not cost us every other tool.

    Real case: a WebCalendar MCP server emitted `"properties": []` for one of nine
    tools (PHP's json_encode turns an empty array into `[]`, but JSON Schema requires
    an object). The SDK validates the whole `ListToolsResult`, so that single defect
    made all nine tools — plus prompts and resources — undiscoverable. A gateway
    fronting servers it does not control has to be more forgiving than the SDK.
    """

    def _validation_error(self) -> Exception:
        """The genuine pydantic error, not a hand-written stand-in.

        Deliberately the *versioned* wire model, not `mcp.types.ListToolsResult`: the
        latter types `inputSchema` as a bare `dict[str, Any]` and accepts this payload
        happily. The strictness lives in the per-revision models
        (`mcp_types._v2025_11_25`), where `properties` is `dict[str, Any]` inside a
        restricted JSON Schema subset — and the hub advertises
        `MCP-Protocol-Version: 2025-11-25`, so that is what real responses are
        validated against. Testing against the lax model would have proved nothing.
        """
        from pydantic import ValidationError

        from mcp_types._v2025_11_25 import ListToolsResult

        try:
            ListToolsResult.model_validate(
                {
                    "tools": [
                        {"name": "ok", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "bad", "inputSchema": {"type": "object", "properties": []}},
                    ]
                }
            )
        except ValidationError as e:
            return e
        raise AssertionError("expected ListToolsResult to reject properties: []")

    _PAYLOAD = {
        "tools": [
            {"name": "ok", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "bad", "inputSchema": {"type": "object", "properties": []}},
        ]
    }

    def _session_that_fails_strictly(self) -> Any:
        """Mirrors the real ClientSession's actual failure shape.

        Both `list_tools` and `send_request` raise: the SDK runs
        `_methods.validate_server_result` against the negotiated revision's model
        *before* applying any caller-supplied `result_type`, so a laxer result model
        does not rescue a malformed payload. Only `_dispatcher.send_raw_request`,
        one rung lower, returns the result unvalidated. An earlier version of this
        double let `send_request` succeed, which made a broken implementation look
        like it worked.
        """
        err = self._validation_error()
        payload = self._PAYLOAD

        class _Dispatcher:
            def __init__(self) -> None:
                self.calls: list[tuple[str, Any]] = []

            async def send_raw_request(
                self, method: str, params: Any = None, opts: Any = None
            ) -> dict:
                self.calls.append((method, params))
                return dict(payload)

        class _Session:
            def __init__(self) -> None:
                self._dispatcher = _Dispatcher()

            async def list_tools(self, params: Any = None) -> Any:
                raise err

            async def send_request(self, request: Any, result_type: Any, **kw: Any) -> Any:
                raise err

        return _Session()

    @pytest.mark.asyncio
    async def test_malformed_tool_no_longer_discards_the_whole_list(self) -> None:
        client = MCPClient("http://localhost:3000/mcp", caller=SERVICE_IDENTITY)
        client._session = self._session_that_fails_strictly()

        result = await client.list("tools/list")

        assert [t["name"] for t in result] == ["ok", "bad"]

    @pytest.mark.asyncio
    async def test_equivalent_empty_schema_is_normalized(self) -> None:
        # `[]` and `{}` both mean "no properties", so coercing loses nothing and stops
        # the hub propagating the defect to stricter clients downstream.
        client = MCPClient("http://localhost:3000/mcp", caller=SERVICE_IDENTITY)
        client._session = self._session_that_fails_strictly()

        result = await client.list("tools/list")

        assert result[1]["inputSchema"]["properties"] == {}

    @pytest.mark.asyncio
    async def test_tolerated_response_is_recorded_as_non_conformant(self) -> None:
        client = MCPClient("http://localhost:3000/mcp", caller=SERVICE_IDENTITY)
        client._session = self._session_that_fails_strictly()

        await client.list("tools/list")

        assert client.schema_issues, "a tolerated defect must be reported, not hidden"
        assert any("properties" in issue for issue in client.schema_issues)

    @pytest.mark.asyncio
    async def test_conformant_response_records_no_issues(self) -> None:
        client = MCPClient("http://localhost:3000/mcp", caller=SERVICE_IDENTITY)
        client._session = MockClientSession()

        result = await client.list("tools/list")

        assert result[0]["name"] == "tool1"
        assert client.schema_issues == []

    @pytest.mark.asyncio
    async def test_transport_failure_is_not_papered_over(self) -> None:
        # Only a schema-validation failure is recoverable. Retrying a dead connection
        # leniently would turn "the server is unreachable" into "the server has no
        # tools", which is a far worse lie than an error.
        class _Session:
            async def list_tools(self, params: Any = None) -> Any:
                raise ConnectionError("connection reset")

        client = MCPClient("http://localhost:3000/mcp", caller=SERVICE_IDENTITY)
        client._session = _Session()  # type: ignore[assignment]

        with pytest.raises(MCPClientError) as exc:
            await client.list("tools/list")

        assert "connection reset" in str(exc.value)
        assert client.schema_issues == []

    @pytest.mark.asyncio
    async def test_missing_dispatcher_reports_the_original_error(self) -> None:
        # `_dispatcher` is private SDK API. If a future version drops it, the fallback
        # must surface the real validation failure rather than claim zero tools.
        err = self._validation_error()

        class _Session:
            async def list_tools(self, params: Any = None) -> Any:
                raise err

        client = MCPClient("http://localhost:3000/mcp", caller=SERVICE_IDENTITY)
        client._session = _Session()  # type: ignore[assignment]

        with pytest.raises(MCPClientError) as exc:
            await client.list("tools/list")

        assert "validation error" in str(exc.value).lower()
