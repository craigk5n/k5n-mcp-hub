import copy
import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from mcp_hub.mcp.discovery import DiscoveryService
from mcp_hub.mcp.schema_refs import resolve_tool_schema_refs
from mcp_hub.mcp.sdk_client import MCPClient
from mcp_hub.mcp.validation import validate_tool_schemas
from mcp_hub.models.server import RegisteredServer
from mcp_hub.registry.service import Registry
from mcp_hub.utils import utcnow

router = APIRouter(prefix="/ui", tags=["ui"])


async def auth_dependency(request: Request) -> None:
    auth_required_dep = request.app.state.auth_required_dependency
    await auth_required_dep(request)


CapabilitiesContext = dict[str, Any]


def _has_cached_capabilities(server: RegisteredServer) -> bool:
    return bool(server.tools or server.prompts or server.resources)


@router.get("/server/{server_id}/tools")
async def get_server_tools(
    request: Request, server_id: str, _: None = Depends(auth_dependency)
) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    templates = request.app.state.templates

    server = await registry.get(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    tools: list[Any] = []
    prompts: list[Any] = []
    resources: list[Any] = []
    cached = False
    last_sync: datetime | None = None
    error_message = ""

    has_cached = _has_cached_capabilities(server)

    if has_cached:
        tools = copy.deepcopy(server.tools or [])
        prompts = copy.deepcopy(server.prompts or [])
        resources = copy.deepcopy(server.resources or [])
        cached = True
        last_sync = server.last_capability_sync
        if tools:
            resolve_tool_schema_refs(tools)
        if server.schema_conformant is None and tools:
            is_conformant, issues = validate_tool_schemas(tools)
            server.schema_conformant = is_conformant
            server.schema_issues = issues
            await registry.register(server)
    else:
        tools, prompts, resources, error_message = await _fetch_live_capabilities(
            server,
            allow_private_networks=request.app.state.settings.security.allow_private_networks,
        )
        cached = False
        last_sync = None
        if error_message and (not tools or not prompts or not resources):
            discovery_service: DiscoveryService = request.app.state.discovery_service
            try:
                await discovery_service.discover_immediately(server, timeout=30)
                tools = server.tools or []
                prompts = server.prompts or []
                resources = server.resources or []
                error_message = ""
            except Exception as e:
                logging.warning("Discovery fallback failed for %s: %s", server_id, e)
        if tools:
            resolve_tool_schema_refs(tools)
        if tools or prompts or resources:
            server.tools = tools
            server.prompts = prompts
            server.resources = resources
            server.last_capability_sync = utcnow()
            if server.schema_conformant is None and tools:
                is_conformant, issues = validate_tool_schemas(tools)
                server.schema_conformant = is_conformant
                server.schema_issues = issues
            await registry.register(server)

    debug_tools_json = json.dumps(tools, indent=2) if tools else None

    last_sync_str = ""
    if last_sync is not None:
        last_sync_str = last_sync.isoformat()

    template = templates.get_template("capabilities.html")
    html = await template.render_async(
        server_id=server_id,
        server_name=server.name,
        url=server.url,
        tools=tools,
        prompts=prompts,
        resources=resources,
        debug_tools_json=debug_tools_json,
        cached=cached,
        last_sync=last_sync_str,
        tool_count=len(tools),
        prompt_count=len(prompts),
        resource_count=len(resources),
        schema_conformant=server.schema_conformant,
        schema_issues=getattr(server, "schema_issues", None) or [],
        error=error_message,
    )
    return HTMLResponse(content=html)


@router.post("/server/{server_id}/refresh-capabilities")
async def refresh_server_capabilities(
    request: Request, server_id: str, _: None = Depends(auth_dependency)
) -> Response:
    registry: Registry = request.app.state.registry
    discovery_service: DiscoveryService = request.app.state.discovery_service

    server = await registry.get(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    try:
        await discovery_service.discover_immediately(server, timeout=30)
    except Exception as e:
        return PlainTextResponse(content=str(e), status_code=400)

    return Response(status_code=204)


@router.get("/server/{server_id}/capabilities")
async def get_server_capabilities(
    request: Request, server_id: str, _: None = Depends(auth_dependency)
) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    templates = request.app.state.templates

    server = await registry.get(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    tools: list[Any] = []
    prompts: list[Any] = []
    resources: list[Any] = []
    cached = False
    last_sync: datetime | None = None
    error_message = ""

    has_cached = _has_cached_capabilities(server)

    if has_cached:
        tools = copy.deepcopy(server.tools or [])
        prompts = copy.deepcopy(server.prompts or [])
        resources = copy.deepcopy(server.resources or [])
        cached = True
        last_sync = server.last_capability_sync
        if tools:
            resolve_tool_schema_refs(tools)
    else:
        tools, prompts, resources, error_message = await _fetch_live_capabilities(
            server,
            allow_private_networks=request.app.state.settings.security.allow_private_networks,
        )
        cached = False
        last_sync = None
        if tools:
            resolve_tool_schema_refs(tools)

    debug_tools_json = json.dumps(tools, indent=2) if tools else None

    last_sync_str = ""
    if last_sync is not None:
        last_sync_str = last_sync.isoformat()

    template = templates.get_template("capabilities.html")
    html = await template.render_async(
        server_id=server_id,
        server_name=server.name,
        url=server.url,
        tools=tools,
        prompts=prompts,
        resources=resources,
        debug_tools_json=debug_tools_json,
        cached=cached,
        last_sync=last_sync_str,
        tool_count=len(tools),
        prompt_count=len(prompts),
        resource_count=len(resources),
        schema_conformant=server.schema_conformant,
        schema_issues=getattr(server, "schema_issues", None) or [],
        error=error_message,
    )
    return HTMLResponse(content=html)


async def _fetch_live_capabilities(
    server: RegisteredServer,
    *,
    allow_private_networks: bool = False,
) -> tuple[list[Any], list[Any], list[Any], str]:
    tools: list[Any] = []
    prompts: list[Any] = []
    resources: list[Any] = []
    error_message = ""
    errors: list[str] = []

    try:
        async with MCPClient(
            server.url, server=server, allow_private_networks=allow_private_networks
        ) as client:
            await client.handshake(timeout=12)

            try:
                tools_result = await client.list("tools/list", timeout=12)
                tools = _extract_list(tools_result)
            except Exception as e:
                logging.warning("Failed to list tools from %s: %s", server.url, e)
                errors.append("tools")

            try:
                prompts_result = await client.list("prompts/list", timeout=12)
                prompts = _extract_list(prompts_result)
            except Exception as e:
                logging.warning("Failed to list prompts from %s: %s", server.url, e)
                errors.append("prompts")

            try:
                resources_result = await client.list("resources/list", timeout=12)
                resources = _extract_list(resources_result)
            except Exception as e:
                logging.warning("Failed to list resources from %s: %s", server.url, e)
                errors.append("resources")

            if errors:
                error_message = f"Failed to fetch: {', '.join(errors)}"

    except Exception as e:
        error_message = f"Handshake failed: {e}"

    return tools, prompts, resources, error_message


def _extract_list(result: Any) -> list[Any]:
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("tools", "prompts", "resources"):
            if key in result:
                value = result[key]
                if isinstance(value, list):
                    return value
    return []
