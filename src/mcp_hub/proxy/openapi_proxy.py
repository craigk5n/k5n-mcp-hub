"""Serve an OpenAPI backend from the proxy route (Story 13.6).

Reached from `proxy/handler.py` after the server access check, the denial trace,
body capture and fault injection -- the same steps every backend gets -- so this
only answers the request and records it:

- one trace entry for the MCP exchange, and one per HTTP request to the REST API
  (sanitized by the executor: no query API key, no Authorization or key header);
- one span and one per-server metric, labelled with the operation's path template
  (`http.route`), never the expanded path (ADR 0009, ADR 0011 §13).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import Request, Response

from mcp_hub.auth.caller import CallerIdentity
from mcp_hub.auth.metadata import bearer_challenge
from mcp_hub.config import TraceConfig
from mcp_hub.models.server import RegisteredServer
from mcp_hub.openapi.executor import CallLimits, UpstreamExchange
from mcp_hub.openapi.virtual_server import CallContext, handle_request
from mcp_hub.trace import Entry, truncate_body


async def serve_openapi(
    request: Request,
    server: RegisteredServer,
    *,
    caller: CallerIdentity,
    request_body: bytes,
    trace_recorder: Any,
    trace_settings: TraceConfig,
    trace: dict[str, Any],
    otel: Any,
    span_attributes: dict[str, object],
    start_time: float,
) -> Response:
    """`trace` carries the entry fields the handler already computed: timestamp,
    subject, incoming url, verbose flag, captured request headers and body."""
    settings = request.app.state.settings
    verbose: bool = trace["verbose"]
    if request.method != "POST":
        # No SSE stream to offer on GET (a body on a GET must not run a tool call).
        return Response(status_code=405, headers={"Allow": "POST"})
    upstream: list[UpstreamExchange] = []
    context = CallContext(
        server=server,
        caller=caller,
        description_limit=settings.openapi.max_description_chars,
        limits=CallLimits.from_settings(settings),
        http_client=getattr(request.app.state, "openapi_http_client", None),
        on_exchange=upstream.append,
    )
    reply = await handle_request(
        request_body,
        context=context,
        protocol_header=request.headers.get("mcp-protocol-version", ""),
    )
    elapsed_ms = (time.perf_counter() - start_time) * 1000

    for exchange in upstream:
        trace_recorder.add(
            Entry(
                timestamp=trace["timestamp"],
                server_id=server.id,
                subject=trace["subject"],
                operation="openapi_call",
                http_method=exchange.method,
                url=trace["incoming_url"],
                outbound_url=exchange.url if verbose else exchange.url_template,
                status=exchange.status,
                duration_ms=exchange.duration_ms,
                error=exchange.error,
                request_headers=exchange.request_headers if verbose else {},
                response_headers=exchange.response_headers if verbose else {},
                request_body=truncate_body(exchange.request_body, trace_settings.body_limit)
                if verbose
                else b"",
            )
        )
    trace_recorder.add(
        Entry(
            timestamp=trace["timestamp"],
            server_id=server.id,
            subject=trace["subject"],
            operation="proxy",
            http_method=request.method,
            url=trace["incoming_url"],
            outbound_url=server.url,
            status=reply.status_code,
            duration_ms=elapsed_ms,
            error=reply.error,
            request_headers=trace["request_headers"],
            request_body=trace["request_body"],
            response_body=truncate_body(reply.body, trace_settings.body_limit) if verbose else b"",
        )
    )
    _record_telemetry(otel, server, reply, span_attributes, elapsed_ms)

    headers: dict[str, str] = {}
    if reply.needs_authentication and settings.auth.type == "jwt":
        headers["WWW-Authenticate"] = bearer_challenge(request, settings.auth)
    media_type = "application/json" if reply.body and reply.status_code != 401 else None
    return Response(
        content=reply.body, status_code=reply.status_code, media_type=media_type, headers=headers
    )


def _record_telemetry(
    otel: Any,
    server: RegisteredServer,
    reply: Any,
    attributes: dict[str, object],
    elapsed_ms: float,
) -> None:
    if otel is None:
        return
    failed = bool(reply.error)
    attributes = {**attributes, "mcp.transport": "openapi"}
    if reply.route:
        attributes["http.route"] = reply.route
    if reply.tool_name:
        attributes["mcp.tool.name"] = reply.tool_name
    with otel.span("mcp.proxy", attributes) as span:
        span.set_attribute("http.response.status_code", reply.status_code)
        if failed:
            # The outcome, not the message: error text is where credentials turn up.
            span.set_attribute("mcp.outcome", "error")
            span.set_status_error()
    otel.record_proxy_request(
        server.id,
        duration_ms=elapsed_ms,
        outcome="error" if failed else "ok",
        transport="openapi",
        error=failed,
        route=reply.route,
    )
