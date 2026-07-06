import json
import logging
import re
import urllib.parse
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from jinja2 import TemplateNotFound

from mcp_hub.mcp.constants import PROTOCOL_VERSION
from mcp_hub.mcp.jsonrpc import build_call_tool_request, build_initialize_request
from mcp_hub.models import RegisteredServer
from mcp_hub.registry.service import Registry
from mcp_hub.routes.ui_invoke import build_tool_args

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"])

VALID_MODES = frozenset({"direct", "hub"})


def sanitize_filename(input_str: str) -> str:
    if not input_str:
        return "download"
    sanitized = re.sub(r"[^A-Za-z0-9_.-]", "_", input_str)
    if not sanitized or sanitized == "-" or sanitized == "." or sanitized == "..":
        sanitized = "download"
    if sanitized.startswith("."):
        sanitized = "_" + sanitized[1:]
    return sanitized


async def auth_dependency(request: Request) -> None:
    auth_required_dep = request.app.state.auth_required_dependency
    await auth_required_dep(request)


def validate_ids(server_id: str, tool_name: str) -> None:
    if not server_id or not re.match(r"^[A-Za-z0-9_.-]+$", server_id):
        raise HTTPException(status_code=400, detail="Invalid server_id")
    if not tool_name or not re.match(r"^[A-Za-z0-9_.-]+$", tool_name):
        raise HTTPException(status_code=400, detail="Invalid tool_name")


async def get_server_or_404(request: Request, server_id: str) -> "RegisteredServer":
    registry: Registry = request.app.state.registry
    try:
        srv = await registry.get(server_id)
    except Exception as e:
        logger.exception("Failed to retrieve server from registry")
        raise HTTPException(status_code=500, detail="Failed to retrieve server") from e
    if srv is None:
        raise HTTPException(status_code=404, detail="Server not found")
    return srv


async def parse_form_args(request: Request) -> tuple[str, dict[str, Any]]:
    form_data = await request.form()
    form_dict: dict[str, list[str]] = {}
    for k, v in form_data.items():
        if isinstance(v, str):
            form_dict.setdefault(k, []).append(v)

    mode_list = form_dict.get("mode", ["direct"])
    mode = mode_list[0] if mode_list else "direct"

    if mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail="Invalid mode. Must be 'direct' or 'hub'")

    try:
        args = build_tool_args(form_dict, ignore={"mode"})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return mode, args


def build_template_context(
    srv: Any,
    tool_name: str,
    args: dict[str, Any],
    mode: str,
    request: Request,
    template_type: str = "shell",
) -> dict[str, Any]:
    protocol_version = srv.mcp_protocol_version or PROTOCOL_VERSION

    init_body = build_initialize_request(
        request_id="1",
        client_name="k5n-mcp-hub-ui",
        client_version="0.1.0",
    )
    init_body["params"]["protocolVersion"] = protocol_version
    init_body_json = json.dumps(init_body)

    call_body = build_call_tool_request(tool_name, args, request_id="3")
    call_body_json = json.dumps(call_body)

    if mode == "direct" and srv.auth_type == "bearer" and srv.bearer_token:
        if template_type == "python":
            auth_header_value = "MCP_BEARER_TOKEN"
        else:
            auth_header_value = "${MCP_BEARER_TOKEN:-}"
    elif mode == "direct" and srv.auth_type == "oauth":
        if template_type == "python":
            auth_header_value = "MCP_ACCESS_TOKEN"
        else:
            auth_header_value = "${MCP_ACCESS_TOKEN:-}"
    else:
        auth_header_value = ""

    if mode == "direct":
        base_url = srv.url
    else:
        settings = request.app.state.settings
        http_port = settings.server.http_port
        http_host = settings.server.http_host
        scheme = "https" if getattr(settings.server, "https", False) else "http"

        if http_host == "0.0.0.0":
            canonical_host = f"localhost:{http_port}"
        else:
            canonical_host = f"{http_host}:{http_port}"

        base_url = f"{scheme}://{canonical_host}/mcp"

    is_hub = mode == "hub"
    target_server_id = srv.id
    is_streamable = srv.mcp_transport == "sse"
    ca_bundle = ""

    return {
        "base_url": base_url,
        "init_body": init_body_json,
        "call_body": call_body_json,
        "auth_header_value": auth_header_value,
        "protocol_version": protocol_version,
        "tool_name": tool_name,
        "is_hub": str(is_hub).lower(),
        "target_server_id": target_server_id,
        "is_streamable": str(is_streamable).lower(),
        "ca_bundle": ca_bundle,
    }


async def render_tool_script(
    request: Request,
    template_name: str,
    context: dict[str, Any],
) -> str:
    templates: Any = request.app.state.templates
    try:
        template = templates.get_template(template_name)
    except TemplateNotFound:
        raise HTTPException(status_code=500, detail="Download template not found")

    try:
        return await template.render_async(**context)
    except KeyError as e:
        logger.exception("Missing template context variable")
        raise HTTPException(
            status_code=500, detail=f"Missing required template variable: {e}"
        ) from e
    except Exception as e:
        logger.exception("Failed to render template")
        raise HTTPException(status_code=500, detail="Failed to generate download") from e


@router.post("/server/{server_id}/tool/{tool_name}/download")
async def download_tool_script(
    request: Request,
    server_id: str,
    tool_name: str,
    _: None = Depends(auth_dependency),
) -> Response:
    validate_ids(server_id, tool_name)
    srv = await get_server_or_404(request, server_id)
    mode, args = await parse_form_args(request)
    context = build_template_context(srv, tool_name, args, mode, request, template_type="shell")
    rendered = await render_tool_script(request, "tool_script.sh.j2", context)

    safe_srv_id = sanitize_filename(server_id)
    safe_tool_name = sanitize_filename(tool_name)
    filename = f"mcp-{safe_srv_id}-{safe_tool_name}.sh"
    filename_encoded = urllib.parse.quote(filename, safe="")

    return Response(
        content=rendered,
        media_type="text/x-shellscript",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{filename_encoded}"
        },
    )


@router.post("/server/{server_id}/tool/{tool_name}/download-python")
async def download_tool_script_python(
    request: Request,
    server_id: str,
    tool_name: str,
    _: None = Depends(auth_dependency),
) -> Response:
    validate_ids(server_id, tool_name)
    srv = await get_server_or_404(request, server_id)
    mode, args = await parse_form_args(request)
    context = build_template_context(srv, tool_name, args, mode, request, template_type="python")
    rendered = await render_tool_script(request, "tool_script.py.j2", context)

    safe_srv_id = sanitize_filename(server_id)
    safe_tool_name = sanitize_filename(tool_name)
    filename = f"mcp-{safe_srv_id}-{safe_tool_name}.py"
    filename_encoded = urllib.parse.quote(filename, safe="")

    return Response(
        content=rendered,
        media_type="text/x-python",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{filename_encoded}"
        },
    )
