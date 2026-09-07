"""Process lifecycle for stdio servers (Epic 9, Story 9.4).

Driven against the real echo fixture and real subprocesses. The thing most likely
to be wrong here is not logic but *ownership*: the SDK's stdio transport is an anyio
task group, so it has to be entered and exited in the same task. A pool that opens a
session during one request and closes it during another violates anyio's cancel-scope
rules and fails with "Attempted to exit a cancel scope that isn't the current task's"
-- the same trap `sdk_client.py` documents for the HTTP transport. Only a test that
holds a session across awaits and then shuts it down catches that.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from mcp_hub.config import StdioCommandConfig, StdioConfig
from mcp_hub.models.server import RegisteredServer

ECHO = Path(__file__).parent / "fixtures" / "echo_stdio_server.py"


def _config(**extra: StdioCommandConfig) -> StdioConfig:
    commands: dict[str, Any] = {
        "echo": StdioCommandConfig(command=sys.executable, args=[str(ECHO)]),
    }
    commands.update(extra)
    return StdioConfig(enabled=True, trusted_network=True, allowed_commands=commands)


def _server(name: str = "echo", server_id: str = "e") -> RegisteredServer:
    return RegisteredServer(id=server_id, url="", transport_kind="stdio", stdio_command_name=name)


def _child_pids() -> set[int]:
    """Our direct children, read from /proc rather than by running `ps`.

    Shelling out to `ps` here counts the `ps` process itself as a child, and it gets
    a different pid on every call -- so a before/after diff always looks like a
    surviving process. That produced a false failure that looked exactly like a
    leaked stdio server.
    """
    me = os.getpid()
    children: set[int] = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path("/proc", entry, "stat").read_text()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue  # exited between listing and reading
        # ppid is field 4, but the comm field (2) may itself contain spaces or
        # parens, so split after the final ')'.
        try:
            ppid = int(stat[stat.rindex(")") + 1 :].split()[1])
        except (ValueError, IndexError):
            continue
        if ppid == me:
            children.add(int(entry))
    return children


class TestStdioPool:
    @pytest.mark.asyncio
    async def test_starts_lazily_and_reuses_one_process(self) -> None:
        from mcp_hub.mcp.stdio_pool import StdioPool

        pool = StdioPool(_config())
        try:
            assert pool.is_running("e") is False, "must not spawn until asked"

            first = await pool.session(_server())
            assert pool.is_running("e") is True
            second = await pool.session(_server())

            assert first is second, "one long-lived process per server, not one per call"
        finally:
            await pool.close_all(timeout=5)

    @pytest.mark.asyncio
    async def test_the_session_actually_works(self) -> None:
        from mcp_hub.mcp.stdio_pool import StdioPool

        pool = StdioPool(_config())
        try:
            session = await pool.session(_server())
            tools = await session.list_tools()
            assert sorted(t.name for t in tools.tools) == ["count_chars", "echo"]

            # Held across awaits and used again: this is where wrong task ownership
            # of the anyio scope shows up.
            await asyncio.sleep(0)
            result = await session.call_tool("echo", {"text": "still here"})
            assert result.content[0].text == "still here"
        finally:
            await pool.close_all(timeout=5)

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_process(self) -> None:
        from mcp_hub.mcp.stdio_pool import StdioPool

        pool = StdioPool(_config())
        try:
            sessions = await asyncio.gather(*[pool.session(_server()) for _ in range(5)])
            assert len({id(s) for s in sessions}) == 1, "single-flight start, not five processes"
        finally:
            await pool.close_all(timeout=5)

    @pytest.mark.asyncio
    @pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc to see child processes")
    async def test_close_all_leaves_no_child_process(self) -> None:
        from mcp_hub.mcp.stdio_pool import StdioPool

        before = _child_pids()
        pool = StdioPool(_config())
        await pool.session(_server())
        assert _child_pids() - before, "the fixture should be running as our child"

        await pool.close_all(timeout=5)
        await asyncio.sleep(0.2)

        assert not (_child_pids() - before), "a stdio server outlived the pool"
        assert pool.is_running("e") is False

    @pytest.mark.asyncio
    async def test_a_server_that_will_not_start_reports_clearly(self) -> None:
        from mcp_hub.mcp.stdio_pool import StdioPool, StdioProcessError

        pool = StdioPool(
            _config(
                dies=StdioCommandConfig(command=sys.executable, args=["-c", "raise SystemExit(3)"])
            )
        )
        try:
            with pytest.raises(StdioProcessError) as exc:
                await pool.session(_server(name="dies", server_id="d"))
            assert "dies" in str(exc.value) or "d" in str(exc.value)
            assert pool.is_running("d") is False, "a failed start must not leave a half-open entry"
        finally:
            await pool.close_all(timeout=5)

    @pytest.mark.asyncio
    async def test_restarts_after_the_process_is_gone(self) -> None:
        from mcp_hub.mcp.stdio_pool import StdioPool

        pool = StdioPool(_config())
        try:
            first = await pool.session(_server())
            await pool.close("e")
            assert pool.is_running("e") is False

            second = await pool.session(_server())
            assert second is not first, "a new process means a new session"
            tools = await second.list_tools()
            assert tools.tools
        finally:
            await pool.close_all(timeout=5)

    @pytest.mark.asyncio
    async def test_unknown_command_is_refused_before_spawning(self) -> None:
        from mcp_hub.mcp.stdio_pool import StdioPool, StdioProcessError

        pool = StdioPool(_config())
        try:
            with pytest.raises(StdioProcessError):
                await pool.session(_server(name="not-allowlisted", server_id="x"))
        finally:
            await pool.close_all(timeout=5)


class TestPoolIsWiredIntoTheApp:
    """A pool nobody shuts down leaks processes for the life of the machine."""

    def test_app_exposes_a_pool(self) -> None:
        from fastapi.testclient import TestClient

        from mcp_hub.app import create_app
        from mcp_hub.config import Settings

        app = create_app(Settings.from_defaults())
        with TestClient(app):
            pool = getattr(app.state, "stdio_pool", None)
            assert pool is not None, "routes read dependencies off app.state"

    def test_shutdown_closes_the_pool(self) -> None:
        from unittest.mock import AsyncMock, patch

        from fastapi.testclient import TestClient

        from mcp_hub.app import create_app
        from mcp_hub.config import Settings

        with patch("mcp_hub.mcp.stdio_pool.StdioPool.close_all", new_callable=AsyncMock) as closed:
            app = create_app(Settings.from_defaults())
            with TestClient(app):
                pass
        assert closed.await_count == 1, "stdio processes must be stopped on shutdown"
