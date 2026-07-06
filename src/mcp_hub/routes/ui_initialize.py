import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from mcp_hub.mcp.auth import apply_server_auth
from mcp_hub.mcp.jsonrpc import build_initialize_request
from mcp_hub.mcp.oauth import format_auth_challenge, parse_www_authenticate
from mcp_hub.models.server import RegisteredServer
from mcp_hub.registry.service import Registry
from mcp_hub.trace.recorder import (
    TraceEntry,
    sanitize_trace_headers,
    trim_trace_body,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"])


async def _get_server_or_404(server_id: str, registry: Registry) -> RegisteredServer | None:
    """Lookup server by id; return None if absent."""
    return await registry.get(server_id)


def _build_init_body(request_id: int | str) -> bytes:
    """Build and JSON-encode the initialize request body."""
    init_body = build_initialize_request(
        request_id=str(request_id),
        client_name="k5n-mcp-hub-ui",
        client_version="0.1.0",
    )
    return json.dumps(init_body).encode("utf-8")


def _redact_request_headers(headers: dict[str, str]) -> dict[str, str]:
    """Create a redacted copy of request headers for display."""
    redacted = dict(headers)
    auth_key = None
    for key in redacted:
        if key.lower() == "authorization":
            auth_key = key
            break
    if auth_key:
        redacted[auth_key] = "Bearer ****"
    return redacted


def _extract_session_id(headers: dict[str, str]) -> str:
    """Extract session_id from response headers."""
    for key, value in headers.items():
        if key.lower() == "mcp-session-id":
            return value
    return ""


def _extract_protocol_version(headers: dict[str, str], body: dict[str, Any]) -> str:
    """Extract protocol_version from response headers or body."""
    for key, value in headers.items():
        if key.lower() == "mcp-protocol-version":
            return value
    result = body.get("result", {})
    pv = result.get("protocolVersion")
    if pv:
        return str(pv)
    if "protocolVersion" in result:
        return str(result["protocolVersion"])
    return ""


def _extract_server_info(body: dict[str, Any]) -> tuple[str, str]:
    """Extract server_name and server_version from body result.serverInfo."""
    result = body.get("result", {})
    server_info = result.get("serverInfo", {})
    server_name = server_info.get("name", "")
    server_version = server_info.get("version", "")
    return server_name, server_version


def _detect_transport(content_type: str | None, body: bytes) -> Literal["http", "sse", ""]:
    """Detect transport from response Content-Type."""
    if content_type and "text/event-stream" in content_type.lower():
        return "sse"
    if body and len(body) > 0:
        return "http"
    return ""


def _parse_response_body(response: httpx.Response) -> tuple[dict[str, Any], str]:
    """Parse response body, handling SSE format if needed."""
    content_type = response.headers.get("Content-Type", "")
    body_bytes = response.content

    if "text/event-stream" in content_type.lower():
        body_str = body_bytes.decode("utf-8", errors="replace")
        lines = body_str.split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data:
                    try:
                        return json.loads(data), content_type
                    except json.JSONDecodeError:
                        continue
        return {}, content_type

    try:
        return response.json(), content_type
    except json.JSONDecodeError:
        return {}, content_type


@router.get("/server/{server_id}/initialize", response_class=HTMLResponse)
async def get_initialize(request: Request, server_id: str) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    templates = request.app.state.templates
    trace_recorder = request.app.state.trace_recorder
    client = httpx.AsyncClient()

    srv = await registry.get(server_id)
    if srv is None:
        raise HTTPException(status_code=404, detail="Server not found")

    init_body = _build_init_body(request_id=1)

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    original_oauth_status = srv.oauth_token_status

    await apply_server_auth(headers, srv)

    if srv.oauth_token_status != original_oauth_status:
        await registry.register(srv)

    redacted_headers = _redact_request_headers(headers)

    start_time = datetime.now(timezone.utc)

    response = None
    response_status = 0
    response_body_bytes = b""
    response_headers_dict: dict[str, str] = {}
    error_msg = ""
    http_error = False

    try:
        response = await client.post(
            srv.url,
            content=init_body,
            headers=headers,
            timeout=20,
        )
    except httpx.TimeoutException as e:
        error_msg = f"Request timeout: {e}"
        http_error = True
    except httpx.HTTPError as e:
        error_msg = str(e)
        http_error = True
    except Exception as e:
        error_msg = str(e)
        http_error = True
    finally:
        await client.aclose()

    end_time = datetime.now(timezone.utc)
    duration_ms = int((end_time - start_time).total_seconds() * 1000)

    if not http_error and response is not None:
        response_status = response.status_code
        response_body_bytes = response.content
        response_headers_dict = dict(response.headers)

    try:
        if response_body_bytes:
            response_body_str = json.dumps(
                json.loads(response_body_bytes.decode("utf-8")),
                indent=2,
            )
        else:
            response_body_str = error_msg if error_msg else ""
    except (json.JSONDecodeError, UnicodeDecodeError):
        response_body_str = (
            response_body_bytes.decode("utf-8", errors="replace")
            if response_body_bytes
            else error_msg
        )

    session_id = _extract_session_id(response_headers_dict)

    parsed_body: dict[str, Any] = {}
    content_type = ""
    if not http_error and response is not None:
        parsed_body, content_type = _parse_response_body(response)

    protocol_version = _extract_protocol_version(response_headers_dict, parsed_body)

    server_name, server_version = _extract_server_info(parsed_body)

    detected_transport = _detect_transport(
        response_headers_dict.get("Content-Type"),
        response_body_bytes,
    )

    if detected_transport and srv.mcp_transport != detected_transport:
        srv.mcp_transport = detected_transport
        await registry.register(srv)

    auth_hint = ""
    if response_status in (401, 403):
        www_auth = response_headers_dict.get("WWW-Authenticate", "")
        challenge = parse_www_authenticate(www_auth)
        auth_hint = format_auth_challenge(challenge)

    trace_entry = TraceEntry(
        timestamp=datetime.now(timezone.utc),
        server_id=server_id,
        operation="initialize",
        http_method="POST",
        url=srv.url,
        outbound_url=srv.url,
        status=response_status,
        duration_ms=duration_ms,
        error=error_msg,
    )

    if srv.trace_verbose:
        trace_entry.request_headers = sanitize_trace_headers(headers)
        trace_entry.outbound_headers = sanitize_trace_headers(headers)
        trace_entry.response_headers = sanitize_trace_headers(response_headers_dict)
        trace_entry.request_body = trim_trace_body(init_body.decode("utf-8"), body_limit=500)
        trace_entry.response_body = trim_trace_body(response_body_str, body_limit=500)

    trace_recorder.add(trace_entry)

    template = templates.get_template("initialize.html")
    html = await template.render_async(
        server_id=server_id,
        url=srv.url,
        request_body=init_body.decode("utf-8"),
        request_headers=json.dumps(redacted_headers, indent=2),
        response_status=response_status,
        response_body=response_body_str,
        response_headers=json.dumps(response_headers_dict, indent=2),
        session_id=session_id,
        protocol_version=protocol_version,
        server_name=server_name,
        server_version=server_version,
        duration_ms=duration_ms,
        error=error_msg,
        auth_hint=auth_hint,
    )
    return HTMLResponse(content=html)
