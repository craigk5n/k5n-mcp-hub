import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from devhub.proxy import build_outbound_headers, proxy_request
from devhub.models.server import RegisteredServer, FaultInjection
from devhub.mcp.constants import PROTOCOL_VERSION
from devhub.config import TraceConfig


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


def _get_header(headers: dict[str, str], key: str) -> str | None:
    lower_key = key.lower()
    for k, v in headers.items():
        if k.lower() == lower_key:
            return v
    return None


class TestBuildOutboundHeaders:
    @pytest.mark.asyncio
    async def test_incoming_authorization_never_reaches_outbound(self) -> None:
        server = make_server(bearer_token="server-token")
        incoming = httpx.Headers({"Authorization": "Bearer incoming-token", "Other": "header"})

        result = await build_outbound_headers(incoming, server)

        assert (
            _get_header(result, "Authorization") is None
            or _get_header(result, "Authorization") == "Bearer server-token"
        )

    @pytest.mark.asyncio
    async def test_server_bearer_token_sets_outbound_authorization(self) -> None:
        server = make_server(bearer_token="my-secret-token")
        incoming = httpx.Headers({})

        result = await build_outbound_headers(incoming, server)

        assert _get_header(result, "Authorization") == "Bearer my-secret-token"

    @pytest.mark.asyncio
    async def test_outbound_contains_mcp_protocol_version(self) -> None:
        server = make_server()
        incoming = httpx.Headers({})

        result = await build_outbound_headers(incoming, server)

        assert _get_header(result, "MCP-Protocol-Version") == PROTOCOL_VERSION

    @pytest.mark.asyncio
    async def test_mcp_protocol_version_not_overwritten_if_present(self) -> None:
        server = make_server()
        custom_version = "2024-01-01"
        incoming = httpx.Headers({"MCP-Protocol-Version": custom_version})

        result = await build_outbound_headers(incoming, server)

        assert _get_header(result, "MCP-Protocol-Version") == custom_version

    @pytest.mark.asyncio
    async def test_x_mcp_target_server_preserved(self) -> None:
        server = make_server()
        incoming = httpx.Headers({"X-MCP-Target-Server": "my-server"})

        result = await build_outbound_headers(incoming, server)

        assert _get_header(result, "X-MCP-Target-Server") == "my-server"

    @pytest.mark.asyncio
    async def test_host_removed_from_outbound(self) -> None:
        server = make_server()
        incoming = httpx.Headers({"Host": "example.com", "Other": "value"})

        result = await build_outbound_headers(incoming, server)

        assert _get_header(result, "Host") is None

    @pytest.mark.asyncio
    async def test_no_server_token_no_authorization_header(self) -> None:
        server = make_server(bearer_token="")
        incoming = httpx.Headers({})

        result = await build_outbound_headers(incoming, server)

        assert _get_header(result, "Authorization") is None

    @pytest.mark.asyncio
    async def test_other_headers_preserved(self) -> None:
        server = make_server(bearer_token="token")
        incoming = httpx.Headers(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Custom-Header": "custom-value",
            }
        )

        result = await build_outbound_headers(incoming, server)

        assert _get_header(result, "Accept") == "application/json"
        assert _get_header(result, "Content-Type") == "application/json"
        assert _get_header(result, "X-Custom-Header") == "custom-value"

    @pytest.mark.asyncio
    async def test_works_with_dict_instead_of_httpx_headers(self) -> None:
        server = make_server(bearer_token="dict-token")
        incoming = {"Accept": "application/json"}

        result = await build_outbound_headers(incoming, server)

        assert _get_header(result, "Authorization") == "Bearer dict-token"
        assert _get_header(result, "Accept") == "application/json"

    @pytest.mark.asyncio
    async def test_incoming_authorization_stripped_even_without_server_token(
        self,
    ) -> None:
        server = make_server(bearer_token="")
        incoming = httpx.Headers({"Authorization": "Bearer should-be-removed"})

        result = await build_outbound_headers(incoming, server)

        assert _get_header(result, "Authorization") is None


class TestProxyRequest:
    @pytest.fixture
    def mock_registry(self):
        registry = MagicMock()
        registry.get = AsyncMock()
        return registry

    @pytest.fixture
    def trace_recorder(self):
        return MagicMock()

    @pytest.fixture
    def trace_config(self):
        return TraceConfig()

    @pytest.fixture
    def mock_request(self):
        request = MagicMock()
        request.headers = {}
        request.method = "GET"
        request.url.path = "/mcp"
        request.url.query = None
        request.stream = AsyncMock(return_value=iter([]))
        return request

    @pytest.mark.asyncio
    async def test_missing_x_mcp_target_server_returns_400(
        self, mock_registry, trace_recorder, trace_config
    ) -> None:
        mock_request = MagicMock()
        mock_request.headers = {}
        mock_request.method = "GET"
        mock_request.url.path = "/mcp"
        mock_request.url.query = None
        mock_request.stream = AsyncMock(return_value=iter([]))

        response = await proxy_request(mock_request, mock_registry, trace_recorder, trace_config)

        assert response.status_code == 400
        assert response.body == b"Missing X-MCP-Target-Server header"

    @pytest.mark.asyncio
    async def test_unknown_target_id_returns_404(
        self, mock_registry, trace_recorder, trace_config
    ) -> None:
        mock_request = MagicMock()
        mock_request.headers = {"X-MCP-Target-Server": "unknown-server"}
        mock_request.method = "GET"
        mock_request.url.path = "/mcp"
        mock_request.url.query = None
        mock_request.stream = AsyncMock(return_value=iter([]))

        mock_registry.get = AsyncMock(return_value=None)

        response = await proxy_request(mock_request, mock_registry, trace_recorder, trace_config)

        assert response.status_code == 404
        assert response.body == b"Server not found"

    @pytest.mark.asyncio
    async def test_valid_request_proxied_to_backend(
        self, mock_registry, trace_recorder, trace_config
    ) -> None:
        mock_request = MagicMock()
        mock_request.headers = {"X-MCP-Target-Server": "test-server", "Accept": "application/json"}
        mock_request.method = "POST"
        mock_request.url.path = "/mcp/tools"
        mock_request.url.query = "foo=bar"

        async def mock_stream():
            yield b'{"jsonrpc":"2.0"}'

        mock_request.stream = mock_stream

        server = RegisteredServer(id="test-server", url="http://backend.example.com")
        mock_registry.get = AsyncMock(return_value=server)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers({"content-type": "application/json"})

        async def mock_aiter_bytes():
            yield b'{"result":[]}'

        mock_response.aiter_bytes = mock_aiter_bytes

        class MockAsyncContextManager:
            def __init__(self, response):
                self.response = response

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        class MockClient:
            def stream(self, method, url, content, headers):
                return MockAsyncContextManager(mock_response)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        with patch("devhub.proxy.handler.httpx.AsyncClient", return_value=MockClient()):
            response = await proxy_request(
                mock_request, mock_registry, trace_recorder, trace_config
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_unreachable_backend_returns_502(
        self, mock_registry, trace_recorder, trace_config
    ) -> None:
        mock_request = MagicMock()
        mock_request.headers = {"X-MCP-Target-Server": "test-server"}
        mock_request.method = "GET"
        mock_request.url.path = "/mcp"
        mock_request.url.query = None

        async def mock_stream():
            yield b""

        mock_request.stream = mock_stream

        server = RegisteredServer(id="test-server", url="http://unreachable.example.com")
        mock_registry.get = AsyncMock(return_value=server)

        with patch("devhub.proxy.handler.httpx.AsyncClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.stream = MagicMock(side_effect=httpx.ConnectError("Connection failed"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            response = await proxy_request(
                mock_request, mock_registry, trace_recorder, trace_config
            )

        assert response.status_code == 502
        assert response.body == b"Backend unreachable"

    @pytest.mark.asyncio
    async def test_fault_injection_short_circuits(
        self, mock_registry, trace_recorder, trace_config
    ) -> None:
        mock_request = MagicMock()
        mock_request.headers = {"X-MCP-Target-Server": "test-server"}
        mock_request.method = "GET"
        mock_request.url.path = "/mcp"
        mock_request.url.query = None

        async def mock_stream():
            yield b""

        mock_request.stream = mock_stream

        server = RegisteredServer(
            id="test-server",
            url="http://backend.example.com",
            fault_injection=FaultInjection(enabled=True, sse_interrupt=True),
        )
        mock_registry.get = AsyncMock(return_value=server)

        response = await proxy_request(mock_request, mock_registry, trace_recorder, trace_config)

        assert response.status_code == 200
        mock_registry.get.assert_called_once()


class TestTraceRecording:
    @pytest.fixture
    def mock_registry(self):
        registry = MagicMock()
        registry.get = AsyncMock()
        return registry

    @pytest.fixture
    def trace_recorder(self):
        recorder = MagicMock()
        recorder.add = MagicMock()
        return recorder

    @pytest.fixture
    def trace_config(self):
        return TraceConfig()

    @pytest.fixture
    def mock_request_base(self):
        def _make_mock_request(target_server: str = "test-server"):
            mock_request = MagicMock()
            mock_request.headers = {
                "X-MCP-Target-Server": target_server,
                "Accept": "application/json",
            }
            mock_request.method = "POST"
            mock_request.url.path = "/mcp/tools"
            mock_request.url.query = "foo=bar"
            mock_request.url.__str__ = lambda self: "http://example.com/mcp/tools?foo=bar"

            async def mock_stream():
                yield b'{"jsonrpc":"2.0"}'

            mock_request.stream = mock_stream
            return mock_request

        return _make_mock_request

    @pytest.mark.asyncio
    async def test_successful_proxy_records_trace_entry(
        self, mock_registry, trace_recorder, trace_config, mock_request_base
    ) -> None:
        mock_request = mock_request_base()

        server = RegisteredServer(id="test-server", url="http://backend.example.com")
        mock_registry.get = AsyncMock(return_value=server)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers({"content-type": "application/json"})

        async def mock_aiter_bytes():
            yield b'{"result":[]}'

        mock_response.aiter_bytes = mock_aiter_bytes

        class MockAsyncContextManager:
            def __init__(self, response):
                self.response = response

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        class MockClient:
            def stream(self, method, url, content, headers):
                return MockAsyncContextManager(mock_response)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        with patch("devhub.proxy.handler.httpx.AsyncClient", return_value=MockClient()):
            response = await proxy_request(
                mock_request, mock_registry, trace_recorder, trace_config
            )

        assert response.status_code == 200
        trace_recorder.add.assert_called_once()
        entry = trace_recorder.add.call_args[0][0]
        assert entry.operation == "proxy"
        assert entry.server_id == "test-server"
        assert entry.status == 200
        assert entry.duration_ms > 0
        assert entry.error == ""

    @pytest.mark.asyncio
    async def test_connection_error_records_trace_with_502(
        self, mock_registry, trace_recorder, trace_config, mock_request_base
    ) -> None:
        mock_request = mock_request_base()

        server = RegisteredServer(id="test-server", url="http://unreachable.example.com")
        mock_registry.get = AsyncMock(return_value=server)

        with patch("devhub.proxy.handler.httpx.AsyncClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.stream = MagicMock(side_effect=httpx.ConnectError("Connection failed"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            response = await proxy_request(
                mock_request, mock_registry, trace_recorder, trace_config
            )

        assert response.status_code == 502
        trace_recorder.add.assert_called_once()
        entry = trace_recorder.add.call_args[0][0]
        assert entry.operation == "proxy"
        assert entry.server_id == "test-server"
        assert entry.status == 502
        assert entry.error != ""

    @pytest.mark.asyncio
    async def test_verbose_mode_redacts_authorization_header(
        self, mock_registry, trace_config, mock_request_base
    ) -> None:
        mock_request = MagicMock()
        mock_request.headers = {
            "X-MCP-Target-Server": "test-server",
            "Accept": "application/json",
            "Authorization": "Bearer secret-token",
        }
        mock_request.method = "POST"
        mock_request.url.path = "/mcp/tools"
        mock_request.url.query = None
        mock_request.url.__str__ = lambda self: "http://example.com/mcp/tools"

        async def mock_stream():
            yield b'{"jsonrpc":"2.0"}'

        mock_request.stream = mock_stream

        trace_recorder = MagicMock()
        trace_recorder.add = MagicMock()

        server = RegisteredServer(
            id="test-server",
            url="http://backend.example.com",
            trace_verbose=True,
            bearer_token="server-token",
        )
        mock_registry.get = AsyncMock(return_value=server)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers(
            {
                "content-type": "application/json",
                "X-Custom": "value",
            }
        )

        async def mock_aiter_bytes():
            yield b'{"result":[]}'

        mock_response.aiter_bytes = mock_aiter_bytes

        class MockAsyncContextManager:
            def __init__(self, response):
                self.response = response

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        class MockClient:
            def stream(self, method, url, content, headers):
                return MockAsyncContextManager(mock_response)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        with patch("devhub.proxy.handler.httpx.AsyncClient", return_value=MockClient()):
            await proxy_request(mock_request, mock_registry, trace_recorder, trace_config)

        entry = trace_recorder.add.call_args[0][0]
        assert entry.request_headers.get("Authorization") == "[REDACTED]"

    @pytest.mark.asyncio
    async def test_verbose_mode_captures_bodies(self, mock_registry, mock_request_base) -> None:
        mock_request = MagicMock()
        mock_request.headers = {
            "X-MCP-Target-Server": "test-server",
            "Accept": "application/json",
        }
        mock_request.method = "POST"
        mock_request.url.path = "/mcp/tools"
        mock_request.url.query = None
        mock_request.url.__str__ = lambda self: "http://example.com/mcp/tools"

        async def mock_stream():
            yield b'{"jsonrpc":"2.0","id":1}'

        mock_request.stream = mock_stream

        trace_config = TraceConfig(body_limit=10000, capture_sse=False)
        trace_recorder = MagicMock()
        trace_recorder.add = MagicMock()

        server = RegisteredServer(
            id="test-server",
            url="http://backend.example.com",
            trace_verbose=True,
        )
        mock_registry.get = AsyncMock(return_value=server)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers({"content-type": "application/json"})

        async def mock_aiter_bytes():
            yield b'{"result":[],"jsonrpc":"2.0"}'

        mock_response.aiter_bytes = mock_aiter_bytes

        class MockAsyncContextManager:
            def __init__(self, response):
                self.response = response

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        class MockClient:
            def stream(self, method, url, content, headers):
                return MockAsyncContextManager(mock_response)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        with patch("devhub.proxy.handler.httpx.AsyncClient", return_value=MockClient()):
            await proxy_request(mock_request, mock_registry, trace_recorder, trace_config)

        entry = trace_recorder.add.call_args[0][0]
        assert entry.request_body == b'{"jsonrpc":"2.0","id":1}'
        assert entry.response_body == b'{"result":[],"jsonrpc":"2.0"}'

    @pytest.mark.asyncio
    async def test_verbose_mode_truncates_body_to_limit(
        self, mock_registry, mock_request_base
    ) -> None:
        mock_request = MagicMock()
        mock_request.headers = {
            "X-MCP-Target-Server": "test-server",
            "Accept": "application/json",
        }
        mock_request.method = "POST"
        mock_request.url.path = "/mcp/tools"
        mock_request.url.query = None
        mock_request.url.__str__ = lambda self: "http://example.com/mcp/tools"

        large_body = b'{"data":"' + b"x" * 20000 + b'"}'

        async def mock_stream():
            yield large_body

        mock_request.stream = mock_stream

        trace_config = TraceConfig(body_limit=100, capture_sse=False)
        trace_recorder = MagicMock()
        trace_recorder.add = MagicMock()

        server = RegisteredServer(
            id="test-server",
            url="http://backend.example.com",
            trace_verbose=True,
        )
        mock_registry.get = AsyncMock(return_value=server)

        large_response = b'{"result":"' + b"y" * 20000 + b'"}'

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers({"content-type": "application/json"})

        async def mock_aiter_bytes():
            yield large_response

        mock_response.aiter_bytes = mock_aiter_bytes

        class MockAsyncContextManager:
            def __init__(self, response):
                self.response = response

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        class MockClient:
            def stream(self, method, url, content, headers):
                return MockAsyncContextManager(mock_response)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        with patch("devhub.proxy.handler.httpx.AsyncClient", return_value=MockClient()):
            await proxy_request(mock_request, mock_registry, trace_recorder, trace_config)

        entry = trace_recorder.add.call_args[0][0]
        assert len(entry.request_body) <= 114
        assert entry.request_body.endswith(b"...[truncated]")
        assert len(entry.response_body) <= 114
        assert entry.response_body.endswith(b"...[truncated]")


class AsyncIteratorMock:
    def __init__(self, obj):
        self._obj = obj

    def __aiter__(self):
        return self

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def __anext__(self):
        raise StopAsyncIteration
