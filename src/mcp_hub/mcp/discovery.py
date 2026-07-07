from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal

from mcp_hub.mcp.constants import MCP_DISCOVERY_INTERVAL_SECONDS, is_supported_protocol_version
from mcp_hub.mcp.sdk_client import MCPClient
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


class DiscoveryService:
    def __init__(self, registry: Registry, *, allow_private_networks: bool = False) -> None:
        self._registry = registry
        self._allow_private_networks = allow_private_networks

    async def discover_immediately(
        self, server: RegisteredServer, *, timeout: float = 30.0
    ) -> None:
        async with MCPClient(
            server.url, server=server, allow_private_networks=self._allow_private_networks
        ) as client:
            await client.handshake()
            if client.initialize_result is not None:
                result = client.initialize_result
                server.mcp_protocol_version = result.protocol_version
                server.mcp_conformant = is_supported_protocol_version(result.protocol_version)
                server.mcp_transport = result.transport

            tools_raw: Any = None
            prompts_raw: Any = None
            resources_raw: Any = None

            try:
                tools_raw = await client.list("tools/list")
            except Exception as e:
                logger.warning("Failed to list tools for %s: %s", server.id, e)

            try:
                prompts_raw = await client.list("prompts/list")
            except Exception as e:
                logger.warning("Failed to list prompts for %s: %s", server.id, e)

            try:
                resources_raw = await client.list("resources/list")
            except Exception as e:
                logger.warning("Failed to list resources for %s: %s", server.id, e)

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

        async def discover_server(server: RegisteredServer) -> None:
            try:
                await self.discover_immediately(server)
            except Exception as e:
                logger.error("Discovery failed for %s: %s", server.id, e)

        await asyncio.gather(*[discover_server(s) for s in servers])

    async def run_forever(self, interval_seconds: int = MCP_DISCOVERY_INTERVAL_SECONDS) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("poll_once failed: %s", e)
            await asyncio.sleep(interval_seconds)
