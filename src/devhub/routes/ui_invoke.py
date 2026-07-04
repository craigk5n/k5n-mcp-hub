import html
import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from devhub.mcp.auth import apply_server_auth
from devhub.mcp.jsonrpc import (
    build_call_tool_request,
    build_initialized_notification,
    build_initialize_request,
)
from devhub.mcp.sse import extract_sse_data
from devhub.registry.service import Registry
from devhub.trace.recorder import (
    TraceEntry,
    sanitize_trace_headers,
    trim_trace_body,
)
from devhub.utils import SafePinnedTransport, is_url_safe_for_discovery

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"])


async def auth_dependency(request: Request) -> None:
    auth_required_dep = request.app.state.auth_required_dependency
    await auth_required_dep(request)


SUCCESS_FRAGMENT_TEMPLATE = """<div class="p-4 rounded-lg border {color_class}">
  <div class="mb-2 flex items-center justify-between gap-2 text-xs text-slate-500">
    <span>Output</span>
    <div class="flex items-center gap-2">
      <span class="text-xs text-emerald-600" data-copy-status aria-live="polite"></span>
      <button type="button"
              class="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
              data-output-copy
              title="Copy output">
        <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <path d="M6 2a2 2 0 00-2 2v8a2 2 0 002 2h6a2 2 0 002-2V6.414a2 2 0 00-.586-1.414l-1.414-1.414A2 2 0 0010.586 3H6z" />
          <path d="M4 8H3a2 2 0 00-2 2v6a2 2 0 002 2h8a2 2 0 002-2v-1h-2v1H3v-6h1V8z" />
        </svg>
        Copy
      </button>
    </div>
  </div>
  <div data-output-view-panel="raw">
    <pre class="whitespace-pre-wrap text-sm" data-tool-output-raw>{message_html_escaped}</pre>
  </div>
  <div class="hidden" data-output-view-panel="json"><pre class="whitespace-pre-wrap text-sm" data-tool-output-json></pre></div>
  <div class="hidden" data-output-view-panel="yaml"><pre class="whitespace-pre-wrap text-sm" data-tool-output-yaml></pre></div>
</div>"""


def coerce_form_value(value: str) -> str | bool | int | float:
    """Coerce a form string value to the appropriate Python type.

    Args:
        value: The string value from the form.

    Returns:
        The value coerced to bool, int, float, or returned as string.
    """
    lower_value = value.lower()
    if lower_value == "true":
        return True
    if lower_value == "false":
        return False

    try:
        return int(value)
    except ValueError:
        pass

    try:
        return float(value)
    except ValueError:
        pass

    return value


def build_tool_args(form: dict[str, list[str]], ignore: set[str] | None = None) -> dict[str, Any]:
    """Convert a URL-decoded multidict form into JSON-RPC arguments dict.

    Args:
        form: A dictionary where keys are field names and values are lists of strings.
        ignore: Optional set of keys to skip.

    Returns:
        A dictionary with converted values suitable for JSON-RPC arguments.

    Raises:
        ValueError: If a JSON-marked field contains invalid JSON.
    """
    if ignore is None:
        ignore = set()

    json_markers: set[str] = set()
    result: dict[str, Any] = {}

    for key in form:
        if key.startswith("__json__"):
            json_markers.add(key[8:])
            continue

        if key in ignore:
            continue

        values = form[key]
        if not values or not values[0]:
            continue

        value = values[0]
        field_name = key

        if field_name in json_markers:
            try:
                result[field_name] = json.loads(value)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON for {field_name}") from e
        elif value.strip().startswith("{") or value.strip().startswith("["):
            try:
                result[field_name] = json.loads(value)
            except json.JSONDecodeError:
                result[field_name] = coerce_form_value(value)
        else:
            result[field_name] = coerce_form_value(value)

    return result


def _build_error_fragment(message: str) -> str:
    """Build an error HTML fragment."""
    escaped = html.escape(message)
    return SUCCESS_FRAGMENT_TEMPLATE.format(
        color_class="bg-red-50 border-red-200 text-red-800",
        message_html_escaped=escaped,
    )


def _build_success_fragment(message: str) -> str:
    """Build a success HTML fragment."""
    escaped = html.escape(message)
    return SUCCESS_FRAGMENT_TEMPLATE.format(
        color_class="bg-green-50 border-green-200 text-green-800",
        message_html_escaped=escaped,
    )


@router.post("/invoke/{server_id}/{tool_name}", response_class=HTMLResponse)
async def invoke_tool(
    request: Request,
    server_id: str,
    tool_name: str,
    _: None = Depends(auth_dependency),
) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    trace_recorder = request.app.state.trace_recorder

    srv = await registry.get(server_id)
    if srv is None:
        raise HTTPException(status_code=404, detail="Server not found")

    _allow_private = bool(
        getattr(getattr(request.app.state, "settings", None), "security", None)
        and request.app.state.settings.security.allow_private_networks
    )
    url_validation_result = await is_url_safe_for_discovery(
        srv.url, require_reachability=False, allow_private=_allow_private
    )
    is_safe = url_validation_result[0]
    url_error = url_validation_result[1]
    if not is_safe:
        return HTMLResponse(content=_build_error_fragment(f"Invalid server URL: {url_error}"))

    form_data = await request.form()
    form_dict: dict[str, list[str]] = {}
    for k, v in form_data.items():
        if isinstance(v, str):
            form_dict.setdefault(k, []).append(v)

    try:
        tool_args = build_tool_args(form_dict)
    except ValueError as e:
        return HTMLResponse(content=_build_error_fragment(str(e)))

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    original_oauth_status = srv.oauth_token_status
    await apply_server_auth(headers, srv)

    if srv.oauth_token_status != original_oauth_status:
        await registry.register(srv)

    trace_request_headers = headers.copy()
    trace_outbound_headers = headers.copy()

    start_time = datetime.now(timezone.utc)

    response_body = b""
    call_response_status = 500
    call_response_headers: dict[str, str] = {}
    network_error = ""
    call_body_bytes = b""

    try:
        async with httpx.AsyncClient(transport=SafePinnedTransport()) as client:
            init_body = build_initialize_request(
                request_id="1",
                client_name="dev-hub-ui",
                client_version="0.1.0",
            )
            init_body_bytes = json.dumps(init_body).encode("utf-8")

            init_response = await client.post(
                srv.url,
                content=init_body_bytes,
                headers=headers,
                timeout=30.0,
            )

            session_id = init_response.headers.get("Mcp-Session-Id", "")
            protocol_version = init_response.headers.get("Mcp-Protocol-Version", "")

            if not protocol_version:
                try:
                    init_result = init_response.json()
                    protocol_version = str(init_result.get("result", {}).get("protocolVersion", ""))
                except json.JSONDecodeError:
                    pass

            if protocol_version and protocol_version != srv.mcp_protocol_version:
                srv.mcp_protocol_version = protocol_version
                await registry.register(srv)

            content_type = init_response.headers.get("Content-Type", "")
            transport: Literal["http", "sse", ""] = (
                "sse" if content_type and "text/event-stream" in content_type.lower() else "http"
            )

            if transport != srv.mcp_transport:
                srv.mcp_transport = transport
                await registry.register(srv)

            if session_id:
                headers["Mcp-Session-Id"] = session_id
            if protocol_version:
                headers["MCP-Protocol-Version"] = protocol_version

            trace_outbound_headers = headers.copy()

            notif_body = build_initialized_notification()
            notif_body_bytes = json.dumps(notif_body).encode("utf-8")

            await client.post(
                srv.url,
                content=notif_body_bytes,
                headers=headers,
                timeout=30.0,
            )

            call_body = build_call_tool_request(tool_name, tool_args, request_id=3)
            call_body_bytes = json.dumps(call_body).encode("utf-8")

            call_response = await client.post(
                srv.url,
                content=call_body_bytes,
                headers=headers,
                timeout=30.0,
            )

            response_content_type = call_response.headers.get("Content-Type", "")
            response_body = call_response.content

            if response_content_type and "text/event-stream" in response_content_type.lower():
                sse_data = extract_sse_data(response_body)
                if sse_data is not None:
                    response_body = sse_data

            call_response_status = call_response.status_code
            call_response_headers = dict(call_response.headers)

            if not (200 <= call_response_status < 300):
                error_text = (
                    response_body.decode("utf-8", errors="replace") if response_body else ""
                )
                network_error = f"HTTP {call_response_status}: {error_text[:200]}"
    except httpx.NetworkError as e:
        network_error = f"Network error: {str(e)}"
    except httpx.TimeoutException as e:
        network_error = f"Timeout: {str(e)}"
    except httpx.InvalidURL as e:
        network_error = f"Invalid URL: {str(e)}"
    except httpx.HTTPError as e:
        network_error = f"HTTP error: {str(e)}"

    end_time = datetime.now(timezone.utc)
    duration_ms = int((end_time - start_time).total_seconds() * 1000)

    if network_error:
        trace_entry = TraceEntry(
            timestamp=datetime.now(timezone.utc),
            server_id=server_id,
            operation="tools/call",
            http_method="POST",
            url=srv.url,
            outbound_url=srv.url,
            status=502,
            duration_ms=duration_ms,
            error=network_error,
        )
        if srv.trace_verbose:
            trace_entry.request_headers = sanitize_trace_headers(trace_request_headers)
            trace_entry.outbound_headers = sanitize_trace_headers(trace_outbound_headers)

        trace_recorder.add(trace_entry)
        return HTMLResponse(content=_build_error_fragment(network_error))

    try:
        result_data = json.loads(response_body)
    except json.JSONDecodeError:
        result_data = {}

    tool_result = result_data.get("result", {})
    content = tool_result.get("content", [])
    is_error = tool_result.get("isError", False) or bool(result_data.get("error"))

    error_msg = ""
    if is_error:
        error_msg = (result_data.get("error") or {}).get("message", "")
        if not error_msg and content:
            error_msg = content[0].get("text", "")

    output_message = ""
    if not is_error:
        if content:
            # Standard MCP: join the text content blocks; fall back to the raw blocks.
            parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("text")]
            output_message = "\n".join(parts) if parts else json.dumps(content, indent=2)
        elif isinstance(tool_result, (dict, list)) and tool_result:
            # Non-conformant server: the result has no content[] array (it returned raw
            # data). Show the result payload rather than a misleading "No output".
            output_message = json.dumps(tool_result, indent=2)
        else:
            output_message = "No output"

    response_status = call_response_status
    call_response_headers_dict = call_response_headers

    trace_entry = TraceEntry(
        timestamp=datetime.now(timezone.utc),
        server_id=server_id,
        operation="tools/call",
        http_method="POST",
        url=srv.url,
        outbound_url=srv.url,
        status=response_status,
        duration_ms=duration_ms,
        error=error_msg if is_error else "",
    )

    if srv.trace_verbose:
        trace_entry.request_headers = sanitize_trace_headers(trace_request_headers)
        trace_entry.outbound_headers = sanitize_trace_headers(trace_outbound_headers)
        trace_entry.response_headers = sanitize_trace_headers(call_response_headers_dict)
        trace_entry.request_body = trim_trace_body(call_body_bytes.decode("utf-8"), body_limit=500)
        trace_entry.response_body = trim_trace_body(
            response_body.decode("utf-8", errors="replace") if response_body else "",
            body_limit=500,
        )

    trace_recorder.add(trace_entry)

    if is_error:
        return HTMLResponse(content=_build_error_fragment(error_msg))
    else:
        return HTMLResponse(content=_build_success_fragment(output_message))
