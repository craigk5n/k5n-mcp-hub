import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from mcp_hub.mcp.constants import (
    is_supported_protocol_version,
    mcp_version_status,
    supported_protocol_versions_str,
)
from mcp_hub.mcp.sdk_client import MCPClient
from mcp_hub.auth.caller import SERVICE_IDENTITY
from mcp_hub.registry.service import Registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"])
api_router = APIRouter(tags=["ui"])


def format_uptime(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        minutes = int(seconds / 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    hours = int(seconds / 3600)
    minutes = int((seconds % 3600) / 60)
    return f"{hours}h {minutes}m"


async def _probe_server_mcp_info(
    server_id: str, url: str, registry: Registry, *, allow_private_networks: bool = False
) -> None:
    try:
        # Fire-and-forget background probe: the request that scheduled it may already
        # be gone, and its results are shared across users (ADR 0004).
        async with MCPClient(
            url, allow_private_networks=allow_private_networks, caller=SERVICE_IDENTITY
        ) as client:
            await client.handshake()
            if client.initialize_result is not None:
                result = client.initialize_result
                protocol_version = result.protocol_version
                transport = result.transport

                server = await registry.get(server_id)
                if server is not None:
                    updated = server.record_protocol_metadata(protocol_version, transport)

                    if updated:
                        await registry.register(server)
                        logger.info(
                            "Updated MCP info for server %s: protocol=%s, transport=%s",
                            server_id,
                            protocol_version,
                            transport,
                        )
    except Exception:
        logger.exception("Failed to probe MCP info for server %s", server_id)


async def _probe_all_servers_task(
    registry: Registry, *, allow_private_networks: bool = False
) -> None:
    servers = await registry.list()
    for server in servers:
        if server.mcp_transport == "":
            await _probe_server_mcp_info(
                server.id, server.url, registry, allow_private_networks=allow_private_networks
            )


def _schedule_background_probe(request: Request) -> None:
    registry: Registry = request.app.state.registry
    allow_private_networks = bool(request.app.state.settings.security.allow_private_networks)

    async def guarded_probe() -> None:
        try:
            await _probe_all_servers_task(registry, allow_private_networks=allow_private_networks)
        except Exception:
            logger.exception("Background probe task failed")

    asyncio.create_task(guarded_probe())


@router.get("/servers", response_class=HTMLResponse)
async def list_servers(request: Request) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    templates = request.app.state.templates

    servers = await registry.list()
    servers_data = [s.sanitize_for_ui().model_dump(mode="json") for s in servers]

    _schedule_background_probe(request)

    template = templates.get_template("servers.html")
    html = await template.render_async(servers=servers_data)
    return HTMLResponse(content=html)


@api_router.get("/api/servers/{server_id}/health-status", response_class=HTMLResponse)
async def get_server_health_status(request: Request, server_id: str) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    templates = request.app.state.templates

    server = await registry.get(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    server_data = server.sanitize_for_ui().model_dump(mode="json")

    server_data["last_checked"] = server.last_checked

    template = templates.get_template("_health_badge.html")
    html = await template.render_async(
        server=server_data,
        format_uptime=format_uptime,
        is_supported_protocol_version=is_supported_protocol_version,
        mcp_version_status=mcp_version_status,
        supported_versions=supported_protocol_versions_str(),
    )
    return HTMLResponse(content=html, media_type="text/html; charset=utf-8")
