"""Long-lived stdio MCP server processes, one per registered server.

Every other backend in this hub is a URL opened per call. A stdio server is a
subprocess with a session on top, and both have to outlive the request that first
needed them — restarting a program for each `tools/call` would be absurd.

The design constraint is ownership, not logic. The SDK's `stdio_client` is an anyio
task group, and anyio requires a cancel scope to be exited by the task that entered
it. Opening a session inside one request and closing it inside another raises
"Attempted to exit a cancel scope that isn't the current task's" — the same trap
`sdk_client.py` documents for the streamable-HTTP transport. So each server gets a
**supervisor task** that opens the transport, hands the live session out, and holds
it open until asked to stop. Callers only ever borrow the session; they never own it.

Process teardown is left to the SDK: `stdio_client`'s exit path already terminates a
stopped server, bounds every wait, and kills survivors. Reimplementing that here
would mean two things racing for the same pid.

See ADR 0007 for why a stdio server runs under a single service identity shared by
all callers, and why the command comes from an operator allowlist rather than a
registration request.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcp_hub.config import StdioConfig
from mcp_hub.models.server import RegisteredServer

if TYPE_CHECKING:
    from mcp.client.session import ClientSession

logger = logging.getLogger(__name__)

# How long to wait for a server to complete its handshake before giving up on it. A
# stdio server that has not initialized by now is either broken or doing something
# it should not be doing at startup.
START_TIMEOUT_SECONDS = 30.0


class StdioProcessError(RuntimeError):
    """A stdio server could not be started, or died."""


@dataclass
class _Entry:
    """One supervised server. `session` is only valid while `ready` is set."""

    task: asyncio.Task[None] | None = None
    session: Any = None
    # Advertised at initialize, dumped with exclude_none so a key is present only if
    # the server actually claimed it -- discovery gates its list calls on this.
    capabilities: dict[str, Any] | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    error: BaseException | None = None
    # Serializes start attempts so five concurrent callers spawn one process, not five.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class StdioPool:
    def __init__(self, config: StdioConfig) -> None:
        self._config = config
        self._entries: dict[str, _Entry] = {}

    def capabilities(self, server_id: str) -> dict[str, Any] | None:
        """What this server advertised at initialize, or None if it is not running."""
        entry = self._entries.get(server_id)
        return entry.capabilities if entry else None

    def is_running(self, server_id: str) -> bool:
        entry = self._entries.get(server_id)
        return bool(entry and entry.ready.is_set() and entry.session is not None)

    async def session(self, server: RegisteredServer) -> ClientSession:
        """The live session for `server`, starting the process if needed."""
        entry = self._entries.setdefault(server.id, _Entry())

        async with entry.lock:
            if entry.ready.is_set() and entry.session is not None:
                return entry.session  # type: ignore[no-any-return]

            # A previous attempt may have failed and left the entry dirty.
            if entry.task is not None and entry.task.done():
                await self._discard(server.id)
                entry = self._entries.setdefault(server.id, _Entry())

            if entry.task is None:
                params = self._parameters(server)
                entry.task = asyncio.create_task(
                    self._supervise(server.id, params, entry),
                    name=f"stdio:{server.id}",
                )

            try:
                await asyncio.wait_for(entry.ready.wait(), timeout=START_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as e:
                await self._discard(server.id)
                raise StdioProcessError(
                    f"stdio server {server.id!r} did not finish its handshake within "
                    f"{START_TIMEOUT_SECONDS:.0f}s"
                ) from e

            if entry.session is None:
                error = entry.error
                await self._discard(server.id)
                raise StdioProcessError(
                    f"stdio server {server.id!r} failed to start: {error}"
                ) from error

            return entry.session  # type: ignore[no-any-return]

    def _parameters(self, server: RegisteredServer) -> Any:
        from mcp.client.stdio import StdioServerParameters

        name = server.stdio_command_name
        allowed = self._config.allowed_commands.get(name)
        if allowed is None:
            # Registration checks this too; checking again here means a record that
            # predates a config change cannot resurrect a command the operator has
            # since removed from the allowlist.
            raise StdioProcessError(
                f"{name!r} is not an allowed stdio command for server {server.id!r}"
            )

        return StdioServerParameters(
            command=allowed.command,
            args=list(allowed.args),
            env=dict(allowed.env) or None,
            cwd=allowed.cwd or None,
        )

    async def _supervise(self, server_id: str, params: Any, entry: _Entry) -> None:
        """Own the transport for one server, start to finish, in a single task."""
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    if init.capabilities is not None:
                        entry.capabilities = init.capabilities.model_dump(
                            exclude_none=True, by_alias=True
                        )
                    entry.session = session
                    entry.ready.set()
                    logger.info("stdio server %s started", server_id)
                    # Hold the scope open in the task that created it. Everything the
                    # callers do happens on the session while this waits here.
                    await entry.stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - recorded and re-raised to the caller
            entry.error = e
            logger.warning("stdio server %s failed: %s", server_id, e)
        finally:
            entry.session = None
            # Wake anyone waiting on a start that is never going to succeed.
            entry.ready.set()

    async def _discard(self, server_id: str) -> None:
        entry = self._entries.pop(server_id, None)
        if entry is None:
            return
        entry.stop.set()
        task = entry.task
        if task is not None and not task.done():
            task.cancel()
            # Bounded: the SDK's own teardown is bounded, so a task still running
            # after this is a bug there rather than something to wait out here.
            await asyncio.wait([task], timeout=5)

    async def close(self, server_id: str) -> None:
        """Stop one server. It restarts on the next `session()`."""
        await self._discard(server_id)

    async def close_all(self, timeout: float = 5.0) -> None:
        """Stop every server. Best-effort, like the rest of shutdown."""
        entries = list(self._entries.items())
        self._entries.clear()

        for _, entry in entries:
            entry.stop.set()

        tasks = [e.task for _, e in entries if e.task is not None and not e.task.done()]
        if not tasks:
            return

        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            # Give cancellation a moment to run the SDK's teardown, then stop caring:
            # shutdown must not block on a server that will not die (see
            # `app._cancel_and_await_tasks`).
            await asyncio.wait(list(pending), timeout=timeout)
            still_running = [t.get_name() for t in pending if not t.done()]
            if still_running:
                logger.warning(
                    "stdio: %d server(s) did not stop cleanly: %s",
                    len(still_running),
                    ", ".join(sorted(still_running)),
                )
