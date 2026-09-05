import json
import logging
import time
from contextlib import AsyncExitStack
from typing import AsyncGenerator, Protocol

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from mcp_hub.auth.caller import CallerIdentity, caller_from_request
from mcp_hub.auth.metadata import bearer_challenge
from mcp_hub.auth.principal import Principal
from mcp_hub.config import TraceConfig
from mcp_hub.mcp.auth import OBOAuthError, apply_server_auth, invalidate_obo_token
from mcp_hub.mcp.constants import STATELESS_PROTOCOL_VERSION, resolve_protocol_version
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
    caller: CallerIdentity,
    allow_private_networks: bool = False,
    body: bytes | None = None,
) -> dict[str, str]:
    """Build outbound headers for the MCP reverse proxy from incoming request headers.

    This function:
    1. Copies incoming request headers.
    2. Deletes Host (httpx sets it).
    3. Deletes incoming Authorization header — hub credentials must NEVER be forwarded.
    4. Injects MCP-Protocol-Version if not already present.
    5. For stateless (2026-07-28) backends, injects the required Mcp-Method /
       Mcp-Name headers derived from the JSON-RPC ``body`` — only when the client
       didn't set them.
    6. Applies server-side auth via apply_server_auth (adds Bearer token if configured).
    7. Preserves X-MCP-Target-Server for diagnostics.

    Args:
        incoming_headers: The incoming request headers.
        server: The registered server to proxy to.
        body: The request body, used to derive Mcp-Method/Mcp-Name for stateless backends.

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

    if (server.mcp_protocol_version or "").strip() == STATELESS_PROTOCOL_VERSION and body:
        _inject_stateless_request_headers(outbound, body)

    await apply_server_auth(
        outbound, server, caller=caller, allow_private_networks=allow_private_networks
    )

    return outbound


def _inject_stateless_request_headers(outbound: dict[str, str], body: bytes) -> None:
    """Fill in the standard request headers 2026-07-28 requires on POSTs
    (``Mcp-Method``, and ``Mcp-Name`` for named calls), derived from the JSON-RPC
    body. Client-supplied values always win."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(parsed, dict):
        return

    method = parsed.get("method")
    if isinstance(method, str) and method and not _has_header(outbound, "Mcp-Method"):
        outbound["Mcp-Method"] = method

    params = parsed.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    if isinstance(name, str) and name and not _has_header(outbound, "Mcp-Name"):
        outbound["Mcp-Name"] = name


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

    caller = caller_from_request(request)

    async def build_headers() -> dict[str, str]:
        return await build_outbound_headers(
            dict(request.headers),
            srv,
            caller=caller,
            allow_private_networks=allow_private_networks,
            body=request_body,
        )

    try:
        outbound_headers = await build_headers()
    except OBOAuthError as exc:
        # Fail closed (ADR 0003): the backend is never called, and we never fall back
        # to the server's own credential, which would run this with the hub's broader
        # rights and look like success.
        return _obo_failure_response(
            request, srv, exc, trace_recorder, request_start_timestamp, incoming_url, start_time
        )

    # The backend connection must outlive this function: the response body is
    # streamed to the client by Starlette AFTER we return, so the client/stream
    # context managers are held on an AsyncExitStack that the body generator
    # closes when the stream ends.
    stack = AsyncExitStack()
    try:
        # Pin the backend connection to a validated IP (SSRF/DNS-rebinding defense) and
        # never follow redirects (a 3xx to an internal URL would bypass the pin). A
        # local-first hub opts into loopback/LAN backends via allow_private_networks.
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
                follow_redirects=False,
                transport=SafePinnedTransport(allow_private_networks=allow_private_networks),
            )
        )
        resp = await stack.enter_async_context(
            client.stream(
                method=request.method,
                url=outbound_url,
                content=request_body,
                headers=outbound_headers,
            )
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
        await stack.aclose()
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
    except BaseException:
        await stack.aclose()
        raise

    if resp.status_code == 401 and srv.auth_type == "obo" and isinstance(caller, Principal):
        # The token was good enough to be issued but the backend rejected it —
        # rotation or revocation at the IdP. Re-exchange exactly once: zero retries
        # makes ordinary expiry user-visible, unbounded retries turn a broken IdP
        # into a request amplifier.
        logger.info("backend rejected the exchanged token for %s; re-exchanging once", srv.id)
        await stack.aclose()
        await invalidate_obo_token(srv, caller)
        try:
            outbound_headers = await build_headers()
        except OBOAuthError as exc:
            return _obo_failure_response(
                request, srv, exc, trace_recorder, request_start_timestamp, incoming_url, start_time
            )

        stack = AsyncExitStack()
        try:
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
                    follow_redirects=False,
                    transport=SafePinnedTransport(allow_private_networks=allow_private_networks),
                )
            )
            resp = await stack.enter_async_context(
                client.stream(
                    method=request.method,
                    url=outbound_url,
                    content=request_body,
                    headers=outbound_headers,
                )
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
            await stack.aclose()
            logger.error(f"Backend unreachable on re-exchange: {e}")
            return Response(content="Backend unreachable", status_code=502)
        except BaseException:
            await stack.aclose()
            raise

    content_type = resp.headers.get("content-type")
    # Tee the stream into the trace instead of draining it first: a long-lived
    # stream (e.g. 2026-07-28 subscriptions/listen) would otherwise never reach
    # the client while verbose capture buffered it to end-of-stream.
    should_capture = verbose and (settings.capture_sse or not is_sse_content_type(content_type))

    if not verbose:
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
        )
        trace_recorder.add(entry)

    async def stream_response_body() -> AsyncGenerator[bytes, None]:
        # Capture at most body_limit + 1 bytes so truncate_body can tell an
        # exactly-at-limit body from an over-limit one and add its marker.
        captured = bytearray()
        cap = settings.body_limit + 1
        try:
            async for chunk in resp.aiter_bytes():
                if should_capture and len(captured) < cap:
                    captured.extend(chunk[: cap - len(captured)])
                yield chunk
        finally:
            if should_capture:
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
                    response_body=truncate_body(bytes(captured), settings.body_limit),
                )
                trace_recorder.add(entry)
            await stack.aclose()

    return StreamingResponse(
        stream_response_body(),
        status_code=resp.status_code,
        headers=dict(resp.headers),
        media_type=content_type,
    )


def _obo_failure_response(
    request: Request,
    server: RegisteredServer,
    exc: OBOAuthError,
    trace_recorder: TraceRecorder,
    timestamp: object,
    incoming_url: str,
    start_time: float,
) -> Response:
    """Turn a failed on-behalf-of exchange into an actionable response.

    A missing user identity is a 401 that points at the hub's protected-resource
    metadata, so a spec-compliant client knows where to authenticate. Anything else
    is a 502: the hub is configured wrong, and saying so beats a bare 401 that sends
    the client off to re-authenticate for no reason.
    """
    if exc.needs_authentication:
        settings = getattr(request.app.state, "settings", None)
        headers = {}
        if settings is not None and settings.auth.type == "jwt":
            headers["WWW-Authenticate"] = bearer_challenge(request, settings.auth)
        status, body = 401, "Unauthorized"
    else:
        headers = {}
        status, body = 502, f"On-behalf-of authentication failed: {exc.detail}"

    trace_recorder.add(
        Entry(
            timestamp=timestamp,  # type: ignore[arg-type]
            server_id=server.id,
            operation="proxy",
            http_method=request.method,
            url=incoming_url,
            outbound_url="",
            status=status,
            duration_ms=(time.perf_counter() - start_time) * 1000,
            error=exc.detail,
        )
    )
    return Response(content=body, status_code=status, headers=headers)
