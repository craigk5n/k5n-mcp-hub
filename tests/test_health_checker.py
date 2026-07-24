import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
import asyncio

from mcp_hub.config import HealthCheckConfig, TraceConfig
from mcp_hub.health import HealthCheckResult, HealthChecker, HealthParser, check_service_health
from mcp_hub.mcp.sdk_client import MCPClientError
from mcp_hub.models.server import RegisteredServer
from mcp_hub.registry.service import Registry
from mcp_hub.trace import TraceRecorder


def make_server(
    id: str = "test-id",
    url: str = "https://test.example.com",
    trace_verbose: bool = False,
    bearer_token: str = "",
) -> RegisteredServer:
    return RegisteredServer(
        id=id,
        url=url,
        trace_verbose=trace_verbose,
        bearer_token=bearer_token,
    )


class TestCheckServiceHealth:
    @pytest.mark.asyncio
    async def test_200_with_healthy_json_returns_healthy_true(self) -> None:
        server = make_server()
        parser = HealthParser()
        recorder = TraceRecorder()

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.text = '{"status": "healthy", "uptime_seconds": 123.45}'
        mock_response.headers = {"content-type": "application/json"}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await check_service_health(
            server,
            parser,
            client=mock_client,
            timeout_seconds=10,
            trace_recorder=recorder,
            trace_capture_sse=False,
            trace_body_limit=1000,
        )

        assert result.healthy is True
        assert result.health_endpoint_worked is True
        assert result.uptime == 123.45
        assert result.got_404 is False

    @pytest.mark.asyncio
    async def test_200_with_unhealthy_json_returns_healthy_false(self) -> None:
        server = make_server()
        parser = HealthParser()
        recorder = TraceRecorder()

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.text = '{"status": "unhealthy"}'
        mock_response.headers = {}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await check_service_health(
            server,
            parser,
            client=mock_client,
            timeout_seconds=10,
            trace_recorder=recorder,
            trace_capture_sse=False,
            trace_body_limit=1000,
        )

        assert result.healthy is False
        assert result.health_endpoint_worked is True

    @pytest.mark.asyncio
    async def test_404_returns_got_404_true(self) -> None:
        server = make_server()
        parser = HealthParser()
        recorder = TraceRecorder()

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 404
        mock_response.text = "Not Found"
        mock_response.headers = {}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await check_service_health(
            server,
            parser,
            client=mock_client,
            timeout_seconds=10,
            trace_recorder=recorder,
            trace_capture_sse=False,
            trace_body_limit=1000,
        )

        assert result.healthy is False
        assert result.got_404 is True
        assert result.health_endpoint_worked is False

    @pytest.mark.asyncio
    async def test_non_200_non_404_returns_healthy_false(self) -> None:
        server = make_server()
        parser = HealthParser()
        recorder = TraceRecorder()

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.headers = {}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await check_service_health(
            server,
            parser,
            client=mock_client,
            timeout_seconds=10,
            trace_recorder=recorder,
            trace_capture_sse=False,
            trace_body_limit=1000,
        )

        assert result.healthy is False
        assert result.got_404 is False

    @pytest.mark.asyncio
    async def test_network_error_returns_healthy_false_and_records_error(self) -> None:
        server = make_server()
        parser = HealthParser()
        recorder = TraceRecorder()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("connection refused"))

        result = await check_service_health(
            server,
            parser,
            client=mock_client,
            timeout_seconds=10,
            trace_recorder=recorder,
            trace_capture_sse=False,
            trace_body_limit=1000,
        )

        assert result.healthy is False
        assert result.got_404 is False

        traces = recorder.list(server.id)
        assert len(traces) == 1
        assert traces[0].error == "connection refused"

    @pytest.mark.asyncio
    async def test_trace_recorded_with_status_and_duration(self) -> None:
        server = make_server()
        parser = HealthParser()
        recorder = TraceRecorder()

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.text = '{"status": "ok"}'
        mock_response.headers = {}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        await check_service_health(
            server,
            parser,
            client=mock_client,
            timeout_seconds=10,
            trace_recorder=recorder,
            trace_capture_sse=False,
            trace_body_limit=1000,
        )

        traces = recorder.list(server.id)
        assert len(traces) == 1
        assert traces[0].operation == "health"
        assert traces[0].http_method == "GET"
        assert traces[0].status == 200
        assert traces[0].duration_ms >= 0

    @pytest.mark.asyncio
    async def test_trace_verbose_includes_headers_and_body(self) -> None:
        server = make_server(trace_verbose=True, bearer_token="secret-token")
        parser = HealthParser()
        recorder = TraceRecorder()

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.text = '{"status": "healthy", "uptime_seconds": 100}'
        mock_response.headers = {"content-type": "application/json"}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        await check_service_health(
            server,
            parser,
            client=mock_client,
            timeout_seconds=10,
            trace_recorder=recorder,
            trace_capture_sse=False,
            trace_body_limit=50,
        )

        traces = recorder.list(server.id)
        assert len(traces) == 1
        assert "Authorization" in traces[0].request_headers
        assert traces[0].response_body != ""

    @pytest.mark.asyncio
    async def test_non_json_200_returns_healthy_false(self) -> None:
        server = make_server()
        parser = HealthParser()
        recorder = TraceRecorder()

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.text = "not json"
        mock_response.headers = {}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await check_service_health(
            server,
            parser,
            client=mock_client,
            timeout_seconds=10,
            trace_recorder=recorder,
            trace_capture_sse=False,
            trace_body_limit=1000,
        )

        assert result.healthy is False
        assert result.health_endpoint_worked is True


class MockStorage:
    def __init__(self, servers: list[RegisteredServer]) -> None:
        self._servers = {s.id: s for s in servers}

    async def init(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get(self, server_id: str) -> RegisteredServer | None:
        return self._servers.get(server_id)

    async def list(self) -> list[RegisteredServer]:
        return list(self._servers.values())

    async def register(self, server: RegisteredServer) -> None:
        self._servers[server.id] = server

    async def unregister(self, server_id: str) -> None:
        self._servers.pop(server_id, None)

    async def save(self, server: RegisteredServer) -> None:
        self._servers[server.id] = server


def make_checker(
    servers: list[RegisteredServer],
    interval_seconds: int = 30,
    timeout_seconds: int = 5,
    failure_threshold: int = 3,
    auto_unregister: bool = False,
) -> tuple[HealthChecker, MockStorage]:
    storage = MockStorage(servers)
    registry = Registry(storage)
    settings = HealthCheckConfig(
        enabled=True,
        interval_seconds=interval_seconds,
        timeout_seconds=timeout_seconds,
        failure_threshold=failure_threshold,
        auto_unregister=auto_unregister,
    )
    trace_recorder = TraceRecorder()
    trace_settings = TraceConfig()
    return HealthChecker(registry, settings, trace_recorder, trace_settings), storage


class TestHealthChecker:
    @pytest.mark.asyncio
    async def test_health_endpoint_returns_healthy(self) -> None:
        import httpx

        server = RegisteredServer(
            id="test-1",
            url="https://test.example.com/mcp",
            supports_health_endpoint=None,
        )
        checker, storage = make_checker([server])

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.text = '{"status": "ok"}'
        mock_response.headers = {}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("mcp_hub.health.checker.httpx.AsyncClient", return_value=mock_client):
            await checker.check_all_once()

        updated = await storage.get("test-1")
        assert updated is not None
        assert updated.healthy is True
        assert updated.supports_health_endpoint is True

    @pytest.mark.asyncio
    async def test_404_sets_supports_health_endpoint_false(self) -> None:
        import httpx

        server = RegisteredServer(
            id="test-2",
            url="https://test.example.com/mcp",
            supports_health_endpoint=None,
        )
        checker, storage = make_checker([server])

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 404
        mock_response.text = "Not Found"
        mock_response.headers = {}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("mcp_hub.health.checker.httpx.AsyncClient", return_value=mock_client):
            await checker.check_all_once()

        updated = await storage.get("test-2")
        assert updated is not None
        assert updated.supports_health_endpoint is False

    @pytest.mark.asyncio
    async def test_404_skips_http_probe_on_subsequent_iteration(self) -> None:
        import httpx

        server = RegisteredServer(
            id="test-3",
            url="https://test.example.com/mcp",
            supports_health_endpoint=False,
        )
        checker, _ = make_checker([server])

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("mcp_hub.health.checker.httpx.AsyncClient", return_value=mock_client):
            await checker.check_all_once()

        mock_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_unregister_after_failure_threshold(self) -> None:
        import httpx

        server = RegisteredServer(
            id="test-4",
            url="https://test.example.com/mcp",
            supports_health_endpoint=None,
            consecutive_fails=2,
        )
        checker, storage = make_checker([server], failure_threshold=3, auto_unregister=True)

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.headers = {}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("mcp_hub.health.checker.httpx.AsyncClient", return_value=mock_client):
            await checker.check_all_once()

        updated = await storage.get("test-4")
        assert updated is None

    @pytest.mark.asyncio
    async def test_run_forever_exits_on_cancellation(self) -> None:
        import httpx

        server = RegisteredServer(
            id="test-5",
            url="https://test.example.com/mcp",
            supports_health_endpoint=None,
        )
        checker, _ = make_checker([server], interval_seconds=1)

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.text = '{"status": "ok"}'
        mock_response.headers = {}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        start_time = asyncio.get_event_loop().time()

        with patch("mcp_hub.health.checker.httpx.AsyncClient", return_value=mock_client):
            task = asyncio.create_task(checker.run_forever())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        elapsed = asyncio.get_event_loop().time() - start_time
        assert elapsed < 1.0


class TestHealthCheckerStartup:
    def test_lifespan_starts_health_checker(self) -> None:
        # Regression: the background health checker must actually be started on app startup,
        # otherwise every server stays "Unknown"/never-checked forever.
        from fastapi.testclient import TestClient

        from mcp_hub.app import create_app

        with patch(
            "mcp_hub.health.checker.HealthChecker.run_forever", new_callable=AsyncMock
        ) as mock_run:
            app = create_app()
            with TestClient(app):
                # A background task was registered during startup...
                assert len(app.state.context.background_tasks) >= 1
            # ...and it ran the checker loop.
            assert mock_run.await_count >= 1


class TestPingRateLimit:
    """A ping that fails with HTTP 429 means the server is up but throttling us — it must be
    treated as healthy (reachable), not marked down."""

    def _fake_mcp_client(self, error: MCPClientError):
        class _FakePingClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def ping(self, timeout: float = 10) -> None:
                raise error

        return _FakePingClient

    async def _run_with_ping_error(self, error: MCPClientError) -> RegisteredServer | None:
        server = RegisteredServer(
            id="rl", url="https://rl.example.com/mcp", supports_health_endpoint=False
        )
        checker, storage = make_checker([server])

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("mcp_hub.health.checker.httpx.AsyncClient", return_value=mock_client),
            patch("mcp_hub.health.checker.MCPClient", self._fake_mcp_client(error)),
        ):
            await checker.check_all_once()
        return await storage.get("rl")

    @pytest.mark.asyncio
    async def test_429_status_code_marks_degraded(self) -> None:
        updated = await self._run_with_ping_error(
            MCPClientError("Ping failed: boom", kind="ping", status_code=429)
        )
        assert updated is not None
        # Reachable (healthy) but flagged rate-limited/degraded, not plain green.
        assert updated.healthy is True
        assert updated.rate_limited is True

    @pytest.mark.asyncio
    async def test_429_message_text_marks_degraded(self) -> None:
        # Some SDK error paths don't preserve the response object; fall back to the message.
        updated = await self._run_with_ping_error(
            MCPClientError("Ping failed: 429 Too Many Requests", kind="ping")
        )
        assert updated is not None
        assert updated.healthy is True
        assert updated.rate_limited is True

    @pytest.mark.asyncio
    async def test_other_ping_failure_stays_unhealthy(self) -> None:
        updated = await self._run_with_ping_error(
            MCPClientError("Ping failed: connection refused", kind="ping", status_code=None)
        )
        assert updated is not None
        assert updated.healthy is False
        assert updated.rate_limited is False
