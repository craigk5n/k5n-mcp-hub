"""Answer an MCP request for an OpenAPI server in-process, for the admin panels.

The Invoke, Initialize and Playground panels on the servers page talk to a backend's
MCP URL directly. An OpenAPI server has none -- its `url` is the synthetic
`openapi:<id>`, because the hub *is* its MCP server (ADR 0011) -- so for these the
panels hand the request to the same `virtual_server.handle_request` the proxy route
uses. That keeps one implementation: tools/call still goes through
`dispatch_tool_call`, the seam Epic 12's policy check wraps.

Each call is traced like the proxy traces one: an entry per HTTP request to the REST
API (path template only unless the server is verbose), and one for the MCP exchange.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import Request

from mcp_hub.auth.caller import caller_from_request
from mcp_hub.auth.principal import Principal
from mcp_hub.models.server import RegisteredServer
from mcp_hub.openapi.executor import CallLimits, UpstreamExchange
from mcp_hub.openapi.virtual_server import CallContext, VirtualReply, handle_request
from mcp_hub.trace import Entry, truncate_body, utcnow


async def call_virtual(
    request: Request,
    server: RegisteredServer,
    body: bytes,
    *,
    operation: str,
    protocol_header: str = "",
) -> VirtualReply:
    """`body` (a JSON-RPC message) answered by the hub's virtual server for `server`."""
    settings = request.app.state.settings
    caller = caller_from_request(request)
    upstream: list[UpstreamExchange] = []
    context = CallContext(
        server=server,
        caller=caller,
        description_limit=settings.openapi.max_description_chars,
        limits=CallLimits.from_settings(settings),
        http_client=getattr(request.app.state, "openapi_http_client", None),
        on_exchange=upstream.append,
    )
    timestamp = utcnow()
    start = time.perf_counter()
    reply = await handle_request(body, context=context, protocol_header=protocol_header)
    elapsed_ms = (time.perf_counter() - start) * 1000

    recorder = request.app.state.trace_recorder
    subject = caller.subject if isinstance(caller, Principal) else ""
    verbose = server.trace_verbose
    body_limit = settings.trace.body_limit
    for exchange in upstream:
        recorder.add(
            Entry(
                timestamp=timestamp,
                server_id=server.id,
                subject=subject,
                operation="openapi_call",
                http_method=exchange.method,
                url=str(request.url.path),
                outbound_url=exchange.url if verbose else exchange.url_template,
                status=exchange.status,
                duration_ms=exchange.duration_ms,
                error=exchange.error,
                request_headers=exchange.request_headers if verbose else {},
                response_headers=exchange.response_headers if verbose else {},
                request_body=truncate_body(exchange.request_body, body_limit) if verbose else b"",
            )
        )
    recorder.add(
        Entry(
            timestamp=timestamp,
            server_id=server.id,
            subject=subject,
            operation=operation,
            http_method="POST",
            url=str(request.url.path),
            outbound_url=server.url,
            status=reply.status_code,
            duration_ms=elapsed_ms,
            error=reply.error,
            request_body=truncate_body(body, body_limit) if verbose else b"",
            response_body=truncate_body(reply.body, body_limit) if verbose else b"",
        )
    )
    return reply


def rpc_body(message: dict[str, Any]) -> bytes:
    return json.dumps(message).encode()
