import logging
import time
from typing import AsyncGenerator, Protocol

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from mcp_hub.config import TraceConfig
from mcp_hub.mcp.auth import apply_server_auth
from mcp_hub.mcp.constants import resolve_protocol_version
from mcp_hub.models.server import RegisteredServer
from mcp_hub.proxy.fault_injection import apply_fault_injection
from mcp_hub.proxy.url import compose_backend_url
from mcp_hub.registry.service import Registry
from mcp_hub.utils import SafePinnedTransport
from mcp_hub.trace import (
    Entry,
    is_sse_content_type,
    sanitize_headers,
    truncate_body,
    utcnow,
)

logger = logging.getLogger(__name__)

TRACE_RECORDER_ATTR = "record_trace"


class TraceRecorder(Protocol):
    def add(self, entry: Entry) -> None: ...


def _has_header(headers: dict[str, str], key: str) -> bool:
    lower_key = key.lower()
    return any(k.lower() == lower_key for k in headers.keys())


async def build_outbound_headers(
    incoming_headers: httpx.Headers | dict[str, str],
    server: RegisteredServer,
    *,
    allow_private_networks: bool = False,
) -> dict[str, str]:
    """Build outbound headers for the MCP reverse proxy from incoming request headers.

    This function:
    1. Copies incoming request headers.
    2. Deletes Host (httpx sets it).
    3. Deletes incoming Authorization header — hub credentials must NEVER be forwarded.
    4. Injects MCP-Protocol-Version if not already present.
    5. Applies server-side auth via apply_server_auth (adds Bearer token if configured).
    6. Preserves X-MCP-Target-Server for diagnostics.

    Args:
        incoming_headers: The incoming request headers.
        server: The registered server to proxy to.

    Returns:
        A dict of outbound headers to send to the backend.
    """
    outbound: dict[str, str] = {}

    if isinstance(incoming_headers, httpx.Headers):
        for key, value in incoming_headers.items():
            outbound[key] = value
    else:
        for key, value in incoming_headers.items():
            outbound[key] = value

    host_keys = [k for k in outbound.keys() if k.lower() == "host"]
    for key in host_keys:
        del outbound[key]

    auth_keys = [k for k in outbound.keys() if k.lower() == "authorization"]
    for key in auth_keys:
        del outbound[key]

    if not _has_header(outbound, "MCP-Protocol-Version"):
        outbound["MCP-Protocol-Version"] = resolve_protocol_version(server.mcp_protocol_version)

    await apply_server_auth(outbound, server, allow_private_networks=allow_private_networks)

    return outbound


async def proxy_request(
    request: Request,
    registry: Registry,
    trace_recorder: TraceRecorder,
    settings: TraceConfig,
    *,
    allow_private_networks: bool = False,
) -> Response:
    target_id = request.headers.get("X-MCP-Target-Server")

    if not target_id:
        return Response(
            content="Missing X-MCP-Target-Server header",
            status_code=400,
        )

    srv = await registry.get(target_id)
    if srv is None:
        return Response(
            content="Server not found",
            status_code=404,
        )

    start_time = time.perf_counter()
    request_start_timestamp = utcnow()
    incoming_url = str(request.url)

    verbose = srv.trace_verbose

    body_chunks: list[bytes] = []
    async for chunk in request.stream():
        body_chunks.append(chunk)
    request_body = b"".join(body_chunks)

    if verbose:
        request_headers = sanitize_headers(dict(request.headers))
        captured_request_body = truncate_body(request_body, settings.body_limit)
    else:
        request_headers = {}
        captured_request_body = b""

    request_dict: dict[str, object] = {
        "headers": dict(request.headers),
        "method": request.method,
        "body": request_body,
    }

    fault_response = await apply_fault_injection(request_dict, srv)
    if fault_response is not None:
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        if verbose:
            response_headers = sanitize_headers(dict(fault_response.headers))
        else:
            response_headers = {}

        entry = Entry(
            timestamp=request_start_timestamp,
            server_id=srv.id,
            operation="proxy",
            http_method=request.method,
            url=incoming_url,
            outbound_url="",
            status=fault_response.status_code,
            duration_ms=elapsed_ms,
            error="",
            request_headers=request_headers,
            response_headers=response_headers,
            request_body=captured_request_body,
            response_body=b"",
        )
        trace_recorder.add(entry)
        return fault_response

    outbound_url = compose_backend_url(
        server_url=srv.url,
        incoming_path=request.url.path,
        incoming_query=request.url.query or None,
    )

    outbound_headers = await build_outbound_headers(
        dict(request.headers), srv, allow_private_networks=allow_private_networks
    )

    try:
        # Pin the backend connection to a validated IP (SSRF/DNS-rebinding defense) and
        # never follow redirects (a 3xx to an internal URL would bypass the pin). A
        # local-first hub opts into loopback/LAN backends via allow_private_networks.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
            follow_redirects=False,
            transport=SafePinnedTransport(allow_private_networks=allow_private_networks),
        ) as client:
            async with client.stream(
                method=request.method,
                url=outbound_url,
                content=request_body,
                headers=outbound_headers,
            ) as resp:
                if verbose:
                    capture_sse = settings.capture_sse
                    content_type = resp.headers.get("content-type")
                    should_capture = capture_sse or not is_sse_content_type(content_type)

                    if should_capture:
                        response_chunks: list[bytes] = []
                        async for chunk in resp.aiter_bytes():
                            response_chunks.append(chunk)
                        response_body = truncate_body(
                            b"".join(response_chunks), settings.body_limit
                        )

                        elapsed_ms = (time.perf_counter() - start_time) * 1000
                        entry = Entry(
                            timestamp=request_start_timestamp,
                            server_id=srv.id,
                            operation="proxy",
                            http_method=request.method,
                            url=incoming_url,
                            outbound_url=outbound_url,
                            status=resp.status_code,
                            duration_ms=elapsed_ms,
                            error="",
                            request_headers=request_headers,
                            response_headers=sanitize_headers(dict(resp.headers)),
                            request_body=captured_request_body,
                            response_body=response_body,
                        )
                        trace_recorder.add(entry)

                        async def stream_response_body() -> AsyncGenerator[bytes, None]:
                            for chunk in response_chunks:
                                yield chunk

                        return StreamingResponse(
                            stream_response_body(),
                            status_code=resp.status_code,
                            headers=dict(resp.headers),
                            media_type=resp.headers.get("content-type"),
                        )

                elapsed_ms = (time.perf_counter() - start_time) * 1000

                if not verbose:
                    entry = Entry(
                        timestamp=request_start_timestamp,
                        server_id=srv.id,
                        operation="proxy",
                        http_method=request.method,
                        url=incoming_url,
                        outbound_url=outbound_url,
                        status=resp.status_code,
                        duration_ms=elapsed_ms,
                        error="",
                    )
                    trace_recorder.add(entry)

                async def stream_response_body() -> AsyncGenerator[bytes, None]:
                    async for chunk in resp.aiter_bytes():
                        yield chunk

                return StreamingResponse(
                    stream_response_body(),
                    status_code=resp.status_code,
                    headers=dict(resp.headers),
                    media_type=resp.headers.get("content-type"),
                )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
        logger.error(f"Backend unreachable: {e}")
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        entry = Entry(
            timestamp=request_start_timestamp,
            server_id=srv.id,
            operation="proxy",
            http_method=request.method,
            url=incoming_url,
            outbound_url=outbound_url,
            status=502,
            duration_ms=elapsed_ms,
            error=str(e),
        )
        trace_recorder.add(entry)
        return Response(
            content="Backend unreachable",
            status_code=502,
        )
