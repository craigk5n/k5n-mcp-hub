from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from mcp_hub.config import HealthCheckConfig, TraceConfig
from mcp_hub.health.parser import HealthParser
from mcp_hub.health.url import build_health_url
from mcp_hub.mcp.auth import apply_server_auth
from mcp_hub.mcp.sdk_client import MCPClient, MCPClientError
from mcp_hub.models.server import RegisteredServer
from mcp_hub.registry.service import Registry
from mcp_hub.trace import TraceEntry, TraceRecorder, utcnow

logger = logging.getLogger(__name__)


@dataclass
class HealthCheckResult:
    healthy: bool
    uptime: float = 0.0
    got_404: bool = False
    health_endpoint_worked: bool = False


async def check_service_health(
    server: RegisteredServer,
    parser: HealthParser,
    *,
    client: httpx.AsyncClient,
    timeout_seconds: int,
    trace_recorder: TraceRecorder,
    trace_capture_sse: bool,
    trace_body_limit: int,
) -> HealthCheckResult:
    health_url = build_health_url(server.url)
    headers: dict[str, str] = {}
    await apply_server_auth(headers, server)

    start_time = time.perf_counter()
    error_message = ""
    status_code = 0
    response_body = ""
    response_headers: dict[str, str] = {}

    try:
        response = await client.get(health_url, headers=headers, timeout=timeout_seconds)
        status_code = response.status_code
        response_body = response.text
        response_headers = dict(response.headers)
    except httpx.HTTPError as e:
        error_message = str(e)
    except Exception as e:
        error_message = str(e)

    duration_ms = int((time.perf_counter() - start_time) * 1000)

    if server.trace_verbose:
        from mcp_hub.trace.recorder import sanitize_trace_headers, trim_trace_body

        request_headers = sanitize_trace_headers(headers)
        sanitized_response_headers = sanitize_trace_headers(response_headers)
        body_to_record = (
            trim_trace_body(response_body, body_limit=trace_body_limit)
            if trace_body_limit > 0
            else ""
        )
    else:
        request_headers = {}
        sanitized_response_headers = {}
        body_to_record = ""

    trace_entry = TraceEntry(
        timestamp=utcnow(),
        server_id=server.id,
        operation="health",
        http_method="GET",
        url=health_url,
        status=status_code,
        duration_ms=duration_ms,
        error=error_message,
        request_headers=request_headers,
        response_headers=sanitized_response_headers,
        response_body=body_to_record,
    )
    trace_recorder.add(trace_entry)

    if error_message:
        return HealthCheckResult(healthy=False)

    if status_code == 404:
        return HealthCheckResult(healthy=False, got_404=True)

    if status_code != 200:
        return HealthCheckResult(healthy=False)

    try:
        parsed = parser.parse(response_body)
    except Exception:
        return HealthCheckResult(healthy=False, health_endpoint_worked=True)

    return HealthCheckResult(
        healthy=parsed.is_healthy(),
        uptime=parsed.uptime_secs,
        health_endpoint_worked=True,
    )


class HealthChecker:
    def __init__(
        self,
        registry: Registry,
        settings: HealthCheckConfig,
        trace_recorder: TraceRecorder,
        trace_settings: TraceConfig,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._trace_recorder = trace_recorder
        self._trace_settings = trace_settings
        self._parser = HealthParser()

    async def run_forever(self) -> None:
        while True:
            await self.check_all_once()
            try:
                await asyncio.sleep(self._settings.interval_seconds)
            except asyncio.CancelledError:
                raise

    async def check_all_once(self) -> None:
        servers = await self._registry.list()
        async with httpx.AsyncClient() as client:
            for srv in servers:
                await self._check_single_server(srv, client)

    async def _check_single_server(self, srv: RegisteredServer, client: httpx.AsyncClient) -> None:
        healthy = False
        uptime = 0.0

        if srv.supports_health_endpoint is not False:
            result = await check_service_health(
                srv,
                self._parser,
                client=client,
                timeout_seconds=self._settings.timeout_seconds,
                trace_recorder=self._trace_recorder,
                trace_capture_sse=self._trace_settings.capture_sse,
                trace_body_limit=self._trace_settings.body_limit,
            )

            if result.got_404:
                await self._registry.set_supports_health_endpoint(srv.id, False)
                healthy = False
                uptime = 0
            elif result.health_endpoint_worked and srv.supports_health_endpoint is None:
                await self._registry.set_supports_health_endpoint(srv.id, True)
                healthy = result.healthy
                uptime = result.uptime
            else:
                healthy = result.healthy
                uptime = result.uptime

        if not healthy:
            try:
                mcp_client = MCPClient(srv.url, server=srv)
                await mcp_client.ping(timeout=10)
                healthy = True
                uptime = 0
            except MCPClientError as e:
                logger.warning("MCP ping fallback failed for %s: %s", srv.id, e)
            except Exception as e:
                logger.warning("MCP ping fallback failed for %s: %s", srv.id, e)

        new_fails = 0 if healthy else srv.consecutive_fails + 1

        await self._registry.update_health_and_uptime(
            srv.id, healthy=healthy, consecutive_fails=new_fails, uptime=uptime
        )

        if (
            not healthy
            and new_fails >= self._settings.failure_threshold
            and self._settings.auto_unregister
        ):
            await self._registry.unregister(srv.id)
            logger.info(
                "Auto-unregistered server %s after %d consecutive failures", srv.id, new_fails
            )
