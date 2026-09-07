"""Discovery and health over stdio (Epic 9, Story 9.5).

A stdio server's tools must arrive through the *same* path as an HTTP server's, so
capability gating, tolerant schema parsing and schema_issues all keep applying. A
parallel implementation would drift, and the drift would be invisible until a stdio
server hit one of those cases.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from mcp_hub.config import StdioCommandConfig, StdioConfig
from mcp_hub.mcp.discovery import DiscoveryService
from mcp_hub.mcp.stdio_pool import StdioPool
from mcp_hub.models.server import RegisteredServer

ECHO = Path(__file__).parent / "fixtures" / "echo_stdio_server.py"


def _pool() -> StdioPool:
    return StdioPool(
        StdioConfig(
            enabled=True,
            trusted_network=True,
            allowed_commands={"echo": StdioCommandConfig(command=sys.executable, args=[str(ECHO)])},
        )
    )


def _server() -> RegisteredServer:
    return RegisteredServer(id="echo", url="", transport_kind="stdio", stdio_command_name="echo")


def _registry() -> Any:
    """The real Registry over in-memory storage, not a stub.

    A hand-rolled double here missed `update_health_and_uptime` and
    `set_supports_health_endpoint`, which is how a test double drifts from the
    interface it stands in for -- the same failure mode that let a broken lenient
    parse pass four tests earlier in this epic. InMemoryStorage is cheap enough that
    there is no reason to fake it.
    """
    from mcp_hub.registry.service import Registry
    from mcp_hub.storage.memory import InMemoryStorage

    return Registry(InMemoryStorage())


class TestStdioDiscovery:
    @pytest.mark.asyncio
    async def test_tools_are_discovered_from_a_real_subprocess(self) -> None:
        pool = _pool()
        service = DiscoveryService(_registry(), stdio_pool=pool)  # type: ignore[arg-type]
        server = _server()
        try:
            await service.discover_immediately(server)
        finally:
            await pool.close_all(timeout=5)

        assert server.tools is not None, "a stdio server must yield its tools"
        assert sorted(t["name"] for t in server.tools) == ["count_chars", "echo"]
        assert server.last_capability_sync is not None

    @pytest.mark.asyncio
    async def test_capability_gating_still_applies(self) -> None:
        """The echo fixture advertises prompts and resources, so both are requested;
        the point is that the same `_advertises` path runs, not that it skips."""
        pool = _pool()
        service = DiscoveryService(_registry(), stdio_pool=pool)  # type: ignore[arg-type]
        server = _server()
        try:
            await service.discover_immediately(server)
        finally:
            await pool.close_all(timeout=5)

        assert server.prompts == [], "advertised but empty, not skipped"
        assert server.resources == []

    @pytest.mark.asyncio
    async def test_schema_conformance_is_recorded(self) -> None:
        pool = _pool()
        service = DiscoveryService(_registry(), stdio_pool=pool)  # type: ignore[arg-type]
        server = _server()
        try:
            await service.discover_immediately(server)
        finally:
            await pool.close_all(timeout=5)

        assert server.schema_conformant is True
        assert server.schema_issues == []

    @pytest.mark.asyncio
    async def test_no_pool_means_a_clear_error_not_an_ssrf_message(self) -> None:
        """Before this story a stdio server was fed to the HTTP path and failed with
        `host '' failed SSRF validation`, which sends an operator looking in entirely
        the wrong place."""
        service = DiscoveryService(_registry())  # type: ignore[arg-type]
        server = _server()

        with pytest.raises(Exception) as exc:
            await service.discover_immediately(server)

        message = str(exc.value).lower()
        assert "ssrf" not in message
        assert "stdio" in message


class TestStdioHealth:
    @pytest.mark.asyncio
    async def test_a_running_server_pings_healthy(self) -> None:
        from mcp_hub.mcp.stdio_pool import StdioPool as _P

        pool = _pool()
        try:
            session = await pool.session(_server())
            result = await session.send_ping()
            assert result is not None
            assert isinstance(pool, _P)
        finally:
            await pool.close_all(timeout=5)


class TestStdioHealthCheck:
    """Liveness for a subprocess is a ping on its session, not an HTTP GET.

    Before this, the checker built `build_health_url("stdio:echo")` and probed it over
    HTTP, which fails for reasons that have nothing to do with the server's health.
    """

    @pytest.mark.asyncio
    async def test_a_live_stdio_server_is_healthy(self) -> None:
        import httpx

        from mcp_hub.config import TraceConfig
        from mcp_hub.health.checker import HealthChecker
        from mcp_hub.trace.recorder import TraceRecorder

        pool = _pool()
        registry = _registry()
        server = _server()
        await registry.register(server)

        checker = HealthChecker(
            registry,  # type: ignore[arg-type]
            __import__("mcp_hub.config", fromlist=["x"]).HealthCheckConfig(),
            TraceRecorder(),
            TraceConfig(),
            stdio_pool=pool,
        )
        try:
            async with httpx.AsyncClient() as client:
                await checker._check_single_server(server, client)
        finally:
            await pool.close_all(timeout=5)

        assert server.healthy is True
        assert server.consecutive_fails == 0

    @pytest.mark.asyncio
    async def test_a_stdio_server_that_cannot_start_is_unhealthy(self) -> None:
        import httpx

        from mcp_hub.config import HealthCheckConfig, StdioCommandConfig, StdioConfig, TraceConfig
        from mcp_hub.health.checker import HealthChecker
        from mcp_hub.trace.recorder import TraceRecorder

        pool = StdioPool(
            StdioConfig(
                enabled=True,
                trusted_network=True,
                allowed_commands={
                    "dies": StdioCommandConfig(
                        command=sys.executable, args=["-c", "raise SystemExit(9)"]
                    )
                },
            )
        )
        registry = _registry()
        server = RegisteredServer(id="d", url="", transport_kind="stdio", stdio_command_name="dies")
        await registry.register(server)

        checker = HealthChecker(
            registry,  # type: ignore[arg-type]
            HealthCheckConfig(),
            TraceRecorder(),
            TraceConfig(),
            stdio_pool=pool,
        )
        try:
            async with httpx.AsyncClient() as client:
                await checker._check_single_server(server, client)
        finally:
            await pool.close_all(timeout=5)

        assert server.healthy is False
        assert server.consecutive_fails == 1


class TestBackgroundLoopsDoNotClobberEachOther:
    """Health and discovery both write the same record on their own timers.

    `Registry.register` replaces the whole record -- only created_at and previously
    discovered capabilities survive -- so a loop that writes a *snapshot* it read
    earlier silently reverts whatever the other loop wrote in between. The HTTP health
    path avoids this with field-scoped updates (`update_health_and_uptime`); the first
    stdio health path did not, and the result was a server visibly flapping between
    healthy and unhealthy every 30 seconds in a running container.
    """

    @pytest.mark.asyncio
    async def test_health_does_not_revert_discovered_tools(self) -> None:
        import httpx

        from mcp_hub.config import HealthCheckConfig, TraceConfig
        from mcp_hub.health.checker import HealthChecker
        from mcp_hub.storage.memory import InMemoryStorage
        from mcp_hub.registry.service import Registry
        from mcp_hub.trace.recorder import TraceRecorder

        registry = _registry()
        pool = _pool()
        server = _server()
        await registry.register(server)

        service = DiscoveryService(registry, stdio_pool=pool)  # type: ignore[arg-type]
        checker = HealthChecker(
            registry, HealthCheckConfig(), TraceRecorder(), TraceConfig(), stdio_pool=pool
        )
        try:
            await service.discover_immediately(await registry.get("echo"))  # type: ignore[arg-type]
            assert (await registry.get("echo")).tools, "precondition: tools discovered"  # type: ignore[union-attr]

            # A health pass holding an object read *before* discovery ran.
            async with httpx.AsyncClient() as client:
                await checker._check_single_server(server, client)
        finally:
            await pool.close_all(timeout=5)

        stored = await registry.get("echo")
        assert stored is not None
        assert stored.tools, "a health pass must not erase discovered capabilities"
        assert stored.healthy is True

    @pytest.mark.asyncio
    async def test_discovery_does_not_revert_health(self) -> None:
        registry = _registry()
        pool = _pool()
        stale = _server()
        await registry.register(stale)

        # Health marks it up, on a different object than the one discovery holds.
        await registry.update_health_and_uptime(
            "echo", healthy=True, consecutive_fails=0, uptime=1.0
        )

        service = DiscoveryService(registry, stdio_pool=pool)  # type: ignore[arg-type]
        try:
            await service.discover_immediately(stale)
        finally:
            await pool.close_all(timeout=5)

        stored = await registry.get("echo")
        assert stored is not None
        assert stored.tools, "precondition: discovery stored its tools"
        assert stored.healthy is True, "discovery must not revert a health result it never saw"
