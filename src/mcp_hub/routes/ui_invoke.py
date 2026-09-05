import html
import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from mcp_hub.auth.caller import caller_from_request
from mcp_hub.mcp.auth import apply_server_auth
from mcp_hub.mcp.constants import STATELESS_PROTOCOL_VERSION
from mcp_hub.mcp.jsonrpc import (
    build_call_tool_request,
    build_initialized_notification,
    build_initialize_request,
)
from mcp_hub.mcp.sse import extract_sse_data
from mcp_hub.mcp.stateless import stateless_meta
from mcp_hub.registry.service import Registry
from mcp_hub.trace.recorder import (
    TraceEntry,
    sanitize_trace_headers,
    trim_trace_body,
)
from mcp_hub.utils import SafePinnedTransport, is_url_safe_for_discovery

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"])


async def auth_dependency(request: Request) -> None:
    auth_required_dep = request.app.state.auth_required_dependency
    await auth_required_dep(request)


# Success result: a toolbar (JSON badge + Pretty/Raw toggle + Copy, all wired by JS on the
# parent servers page since scripts inside htmx-swapped fragments don't run) over a <pre>
# that wraps long lines and scrolls instead of overflowing the panel. When the output parses
# as JSON, the page pretty-prints it and reveals the toggle.
RESULT_FRAGMENT_TEMPLATE = """<div class="rounded-lg border border-green-200 bg-green-50" data-tool-result>
  <div class="flex items-center justify-between gap-2 px-3 py-2 border-b border-green-100 text-xs">
    <div class="flex items-center gap-2">
      <span class="text-slate-500">Output</span>
      <span class="hidden rounded bg-slate-200 px-1.5 py-0.5 font-medium text-slate-600" data-json-badge>JSON</span>
    </div>
    <div class="flex items-center gap-2">
      <div class="hidden items-center overflow-hidden rounded-md border border-slate-300 text-slate-600" data-format-toggle>
        <button type="button" class="px-2 py-0.5" data-view="pretty">Pretty</button>
        <button type="button" class="border-l border-slate-300 px-2 py-0.5" data-view="raw">Raw</button>
      </div>
      <span class="text-emerald-600" data-copy-status aria-live="polite"></span>
      <button type="button"
              class="inline-flex items-center gap-1 rounded border border-slate-300 bg-white px-2 py-0.5 text-slate-600 hover:bg-slate-50"
              data-output-copy title="Copy output">Copy</button>
    </div>
  </div>
  <pre class="m-0 max-h-96 overflow-auto whitespace-pre-wrap break-words px-3 py-2 text-sm text-slate-800" data-tool-output>{message_html_escaped}</pre>
</div>"""

ERROR_FRAGMENT_TEMPLATE = """<div class="rounded-lg border border-red-200 bg-red-50 px-4 py-3">
  <pre class="m-0 max-h-96 overflow-auto whitespace-pre-wrap break-words text-sm text-red-800" data-tool-output>{message_html_escaped}</pre>
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
    return ERROR_FRAGMENT_TEMPLATE.format(message_html_escaped=escaped)


def _build_success_fragment(message: str) -> str:
    """Build a success HTML fragment."""
    escaped = html.escape(message)
    return RESULT_FRAGMENT_TEMPLATE.format(message_html_escaped=escaped)


# tool_name is a :path converter: MCP tool names may contain "/" (e.g.
# "webcalendar/list-events"), which would otherwise split into extra path segments and
# 404. server_id is a single segment, so the greedy tool_name captures everything after it.
@router.post("/invoke/{server_id}/{tool_name:path}", response_class=HTMLResponse)
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
    await apply_server_auth(
        headers, srv, caller=caller_from_request(request), allow_private_networks=_allow_private
    )

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

    stateless = (srv.mcp_protocol_version or "").strip() == STATELESS_PROTOCOL_VERSION

    try:
        async with httpx.AsyncClient(
            transport=SafePinnedTransport(allow_private_networks=_allow_private)
        ) as client:
            if stateless:
                # 2026-07-28: no handshake, no session — one self-contained POST whose
                # params._meta carries the protocol version and client identity.
                headers["MCP-Protocol-Version"] = STATELESS_PROTOCOL_VERSION
                headers["Mcp-Method"] = "tools/call"
                trace_outbound_headers = headers.copy()

                call_body = build_call_tool_request(tool_name, tool_args, request_id=1)
                call_body["params"]["_meta"] = stateless_meta()
            else:
                init_body = build_initialize_request(
                    request_id="1",
                    client_name="k5n-mcp-hub-ui",
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
                        protocol_version = str(
                            init_result.get("result", {}).get("protocolVersion", "")
                        )
                    except json.JSONDecodeError:
                        pass

                content_type = init_response.headers.get("Content-Type", "")
                transport: Literal["http", "sse", ""] = (
                    "sse"
                    if content_type and "text/event-stream" in content_type.lower()
                    else "http"
                )

                if srv.record_protocol_metadata(protocol_version, transport):
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
