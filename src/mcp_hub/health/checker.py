from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from mcp_hub.config import HealthCheckConfig, TraceConfig
from mcp_hub.health.parser import HealthParser
from mcp_hub.health.url import build_health_url
from mcp_hub.auth.caller import SERVICE_IDENTITY
from mcp_hub.mcp.auth import apply_server_auth, needs_user_identity
from mcp_hub.mcp.constants import STATELESS_PROTOCOL_VERSION
from mcp_hub.mcp.sdk_client import MCPClient, MCPClientError
from mcp_hub.mcp.stateless import StatelessMCPClient
from mcp_hub.models.server import RegisteredServer
from mcp_hub.registry.service import Registry
from mcp_hub.trace import TraceEntry, TraceRecorder, utcnow
from mcp_hub.utils import SafePinnedTransport

logger = logging.getLogger(__name__)


def _is_rate_limited(err: MCPClientError) -> bool:
    """True when a ping failure was actually an HTTP 429 (server up, throttling us) rather
    than the server being unreachable. Checks the recovered status code first, then falls back
    to the message text since some SDK error paths don't preserve the response object."""
    if err.status_code == 429:
        return True
    text = str(err)
    return "429" in text or "Too Many Requests" in text


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
    allow_private_networks: bool = False,
) -> HealthCheckResult:
    health_url = build_health_url(server.url)
    headers: dict[str, str] = {}
    # No user in scope on a timer-driven probe (ADR 0004).
    await apply_server_auth(
        headers, server, caller=SERVICE_IDENTITY, allow_private_networks=allow_private_networks
    )

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
        *,
        allow_private_networks: bool = False,
        stdio_pool: Any = None,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._trace_recorder = trace_recorder
        self._trace_settings = trace_settings
        self._allow_private_networks = allow_private_networks
        self._stdio_pool = stdio_pool
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
        # Pin every health probe to a validated IP (SSRF/DNS-rebinding defense) and never
        # follow redirects; local-first deployments opt into loopback/LAN via the flag.
        async with httpx.AsyncClient(
            follow_redirects=False,
            transport=SafePinnedTransport(allow_private_networks=self._allow_private_networks),
        ) as client:
            for srv in servers:
                await self._check_single_server(srv, client)

    async def _check_stdio_server(self, srv: RegisteredServer) -> None:
        """Liveness for a subprocess: is the process up, and does it answer a ping?

        There is no URL to GET. Probing `build_health_url("stdio:echo")` over HTTP,
        which is what happened before this existed, fails for reasons that say nothing
        about the server. Starting the process if it is not already running is
        deliberate -- a stdio server is only "down" if it cannot be run.
        """
        healthy = False
        error = ""

        if self._stdio_pool is None:
            error = "no stdio pool configured"
        else:
            try:
                session = await self._stdio_pool.session(srv)
                await asyncio.wait_for(session.send_ping(), timeout=self._settings.timeout_seconds)
                healthy = True
            except Exception as e:
                error = str(e)
                logger.warning("stdio health check failed for %s: %s", srv.id, e)

        # Field-scoped updates, not `register(srv)`. Registering writes the whole
        # object, so it would stamp every other field from a snapshot read before the
        # probe -- reverting anything discovery wrote in between. The HTTP path has
        # always used these helpers for exactly that reason.
        consecutive_fails = 0 if healthy else srv.consecutive_fails + 1
        await self._registry.update_health_and_uptime(
            srv.id,
            healthy=healthy,
            consecutive_fails=consecutive_fails,
            uptime=0.0,
        )
        # A stdio server has no /health endpoint to discover, ever.
        if srv.supports_health_endpoint is not False:
            await self._registry.set_supports_health_endpoint(srv.id, False)

        # Keep the caller's object in step with what was just persisted.
        srv.healthy = healthy
        srv.consecutive_fails = consecutive_fails
        srv.supports_health_endpoint = False

        if not healthy and self._settings.auto_unregister:
            if srv.consecutive_fails >= self._settings.failure_threshold:
                logger.warning("auto-unregistering unhealthy stdio server %s: %s", srv.id, error)
                await self._registry.unregister(srv.id)

    async def _check_single_server(self, srv: RegisteredServer, client: httpx.AsyncClient) -> None:
        healthy = False
        uptime = 0.0
        rate_limited = False

        if srv.is_stdio:
            await self._check_stdio_server(srv)
            return

        if srv.supports_health_endpoint is not False:
            result = await check_service_health(
                srv,
                self._parser,
                client=client,
                timeout_seconds=self._settings.timeout_seconds,
                trace_recorder=self._trace_recorder,
                trace_capture_sse=self._trace_settings.capture_sse,
                trace_body_limit=self._trace_settings.body_limit,
                allow_private_networks=self._allow_private_networks,
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
                await self._mcp_probe(srv)
                healthy = True
                uptime = 0
            except MCPClientError as e:
                # A 429 means the server is up but throttling us (often the hub's own frequent
                # authenticated health pings). "Reachable but rate-limited" is healthy, not
                # down — otherwise a busy server flaps red even though it is clearly alive.
                if _is_rate_limited(e):
                    logger.info(
                        "Server %s is rate-limited (429) but reachable; marking degraded",
                        srv.id,
                    )
                    healthy = True
                    rate_limited = True
                    uptime = 0
                else:
                    logger.warning("MCP ping fallback failed for %s: %s", srv.id, e)
            except Exception as e:
                logger.warning("MCP ping fallback failed for %s: %s", srv.id, e)

        new_fails = 0 if healthy else srv.consecutive_fails + 1

        await self._registry.update_health_and_uptime(
            srv.id,
            healthy=healthy,
            consecutive_fails=new_fails,
            uptime=uptime,
            rate_limited=rate_limited,
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

    async def _mcp_probe(self, srv: RegisteredServer) -> None:
        """MCP-level liveness probe, used when the HTTP /health endpoint isn't available.

        Stateless (2026-07-28) servers have no ``ping`` — and no ``initialize``, which is
        what the legacy ping actually performs — so they are probed with ``server/discover``,
        the spec's designated up-front probe."""
        if needs_user_identity(srv):
            # Nothing to authenticate as: an MCP probe would draw a 401 and mark a
            # perfectly reachable server unhealthy. The HTTP reachability check that
            # precedes this still ran, which is all we can honestly assert (ADR 0004).
            logger.debug(
                "server %s is on-behalf-of only; skipping the MCP probe (no service "
                "credential to probe with)",
                srv.id,
            )
            return

        if (srv.mcp_protocol_version or "").strip() == STATELESS_PROTOCOL_VERSION:
            stateless_client = StatelessMCPClient(
                srv.url,
                server=srv,
                allow_private_networks=self._allow_private_networks,
                caller=SERVICE_IDENTITY,
            )
            await stateless_client.discover(timeout=10)
            return

        mcp_client = MCPClient(
            srv.url,
            server=srv,
            allow_private_networks=self._allow_private_networks,
            caller=SERVICE_IDENTITY,
        )
        await mcp_client.ping(timeout=10)
