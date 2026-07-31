from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Literal

from mcp_hub.mcp.constants import (
    MCP_DISCOVERY_INTERVAL_SECONDS,
    STATELESS_PROTOCOL_VERSION,
)
from mcp_hub.mcp.sdk_client import MCPClient, MCPClientError
from mcp_hub.mcp.stateless import StatelessMCPClient
from mcp_hub.mcp.validation import validate_tool_schemas
from mcp_hub.registry.service import Registry
from mcp_hub.utils import utcnow

if TYPE_CHECKING:
    from mcp_hub.models.server import RegisteredServer

logger = logging.getLogger(__name__)


def extract_list_payload(
    raw: Any, kind: Literal["tools", "prompts", "resources"]
) -> list[Any] | None:
    if isinstance(raw, dict):
        error = raw.get("error")
        if isinstance(error, dict) and error.get("code") is not None:
            return None

    if isinstance(raw, list):
        return raw

    if isinstance(raw, dict):
        kind_list = raw.get(kind)
        if isinstance(kind_list, list):
            return kind_list

        result = raw.get("result")
        if isinstance(result, list):
            return result

        data = raw.get("data")
        if isinstance(data, list):
            return data

        if isinstance(result, dict):
            kind_result = result.get(kind)
            if isinstance(kind_result, list):
                return kind_result

            for value in result.values():
                if isinstance(value, list):
                    return value

        for value in raw.values():
            if isinstance(value, list):
                return value

    return None


def extract_ttl_ms(raw: Any) -> float | None:
    """Read the ``ttlMs`` freshness hint from a 2026-07-28 list result, if present."""
    if isinstance(raw, dict):
        ttl = raw.get("ttlMs")
        if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl >= 0:
            return float(ttl)
    return None


class DiscoveryService:
    def __init__(self, registry: Registry, *, allow_private_networks: bool = False) -> None:
        self._registry = registry
        self._allow_private_networks = allow_private_networks
        # Per-server earliest next poll (monotonic seconds), from ttlMs freshness
        # hints on stateless list results. Only poll_once honors this — an explicit
        # discover_immediately call always runs.
        self._poll_not_before: dict[str, float] = {}

    async def discover_immediately(
        self, server: RegisteredServer, *, timeout: float = 30.0
    ) -> None:
        # Probe `server/discover` (2026-07-28) first unless the server is already
        # known to speak a handshake revision — the recorded version acts as a
        # cache so legacy servers aren't re-probed on every poll.
        recorded = (server.mcp_protocol_version or "").strip()
        if not recorded or recorded == STATELESS_PROTOCOL_VERSION:
            if await self._discover_stateless(server, timeout=timeout):
                return
        await self._discover_handshake(server, timeout=timeout)

    async def _discover_stateless(self, server: RegisteredServer, *, timeout: float) -> bool:
        """Try the stateless (2026-07-28) path. Returns False when the server doesn't
        support it (or can't be reached that way), so the caller falls back to the
        `initialize` handshake."""
        client = StatelessMCPClient(
            server.url, server=server, allow_private_networks=self._allow_private_networks
        )
        try:
            discovered = await client.discover(timeout=timeout)
        except MCPClientError as e:
            if e.is_method_not_found:
                logger.info(
                    "Server %s does not implement server/discover; using initialize", server.id
                )
            else:
                logger.warning(
                    "Stateless discover failed for %s (%s); trying initialize", server.id, e
                )
            return False

        # Stateless servers run over Streamable HTTP ("sse" in this hub's transport enum).
        server.record_protocol_metadata(discovered.protocol_version, transport="sse")

        tools_raw: Any = None
        prompts_raw: Any = None
        resources_raw: Any = None

        try:
            tools_raw = await client.list("tools/list", timeout=timeout)
        except Exception as e:
            logger.warning("Failed to list tools for %s: %s", server.id, e)

        try:
            prompts_raw = await client.list("prompts/list", timeout=timeout)
        except Exception as e:
            logger.warning("Failed to list prompts for %s: %s", server.id, e)

        try:
            resources_raw = await client.list("resources/list", timeout=timeout)
        except Exception as e:
            logger.warning("Failed to list resources for %s: %s", server.id, e)

        # The shortest ttlMs across the list results bounds how long everything
        # stays fresh; don't background-repoll before it expires.
        ttls = [
            ttl
            for ttl in (extract_ttl_ms(r) for r in (tools_raw, prompts_raw, resources_raw))
            if ttl is not None
        ]
        if ttls:
            self._poll_not_before[server.id] = time.monotonic() + min(ttls) / 1000.0
        else:
            self._poll_not_before.pop(server.id, None)

        await self._store_capabilities(server, tools_raw, prompts_raw, resources_raw)
        return True

    async def _discover_handshake(self, server: RegisteredServer, *, timeout: float) -> None:
        # Handshake revisions carry no ttl hints; drop any stale pacing entry.
        self._poll_not_before.pop(server.id, None)
        async with MCPClient(
            server.url, server=server, allow_private_networks=self._allow_private_networks
        ) as client:
            # Bound every network call by `timeout`. Previously this argument was ignored and
            # each sub-call fell back to its own 30s default, so a slow/hanging backend could
            # block discovery (and, when called synchronously, the caller) for minutes.
            await client.handshake(timeout=timeout)
            if client.initialize_result is not None:
                result = client.initialize_result
                server.record_protocol_metadata(result.protocol_version, transport=result.transport)

            tools_raw: Any = None
            prompts_raw: Any = None
            resources_raw: Any = None

            try:
                tools_raw = await client.list("tools/list", timeout=timeout)
            except Exception as e:
                logger.warning("Failed to list tools for %s: %s", server.id, e)

            try:
                prompts_raw = await client.list("prompts/list", timeout=timeout)
            except Exception as e:
                logger.warning("Failed to list prompts for %s: %s", server.id, e)

            try:
                resources_raw = await client.list("resources/list", timeout=timeout)
            except Exception as e:
                logger.warning("Failed to list resources for %s: %s", server.id, e)

            await self._store_capabilities(server, tools_raw, prompts_raw, resources_raw)

    async def _store_capabilities(
        self,
        server: RegisteredServer,
        tools_raw: Any,
        prompts_raw: Any,
        resources_raw: Any,
    ) -> None:
        tools = extract_list_payload(tools_raw, "tools")
        prompts = extract_list_payload(prompts_raw, "prompts")
        resources = extract_list_payload(resources_raw, "resources")

        if tools is not None:
            is_conformant, issues = validate_tool_schemas(tools)
            server.schema_conformant = is_conformant
            server.schema_issues = issues
        else:
            server.schema_conformant = None
            server.schema_issues = []

        server.tools = tools
        server.prompts = prompts
        server.resources = resources
        server.last_capability_sync = utcnow()

        await self._registry.register(server)

        if (
            (tools is None or tools == [])
            and (prompts is None or prompts == [])
            and (resources is None or resources == [])
        ):
            raise RuntimeError("discovery completed but no capabilities were found")

    async def poll_once(self) -> None:
        servers = await self._registry.list()

        # Drop pacing state for servers that are no longer registered.
        current_ids = {s.id for s in servers}
        for stale_id in set(self._poll_not_before) - current_ids:
            del self._poll_not_before[stale_id]

        now = time.monotonic()
        due = [s for s in servers if now >= self._poll_not_before.get(s.id, 0.0)]

        async def discover_server(server: RegisteredServer) -> None:
            try:
                await self.discover_immediately(server)
            except Exception as e:
                logger.error("Discovery failed for %s: %s", server.id, e)

        await asyncio.gather(*[discover_server(s) for s in due])

    async def run_forever(self, interval_seconds: int = MCP_DISCOVERY_INTERVAL_SECONDS) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("poll_once failed: %s", e)
            await asyncio.sleep(interval_seconds)
