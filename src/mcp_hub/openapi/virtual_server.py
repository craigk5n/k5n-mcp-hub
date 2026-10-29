"""The MCP server the hub itself is for an OpenAPI backend (Story 13.6, ADR 0011 §9).

One JSON-RPC request in, one reply out: no session, no SSE stream. It speaks both
protocol families -- the handshake revisions (`initialize`) and the stateless
2026-07-28 revision (`server/discover`, results carrying `resultType`, list results
carrying `ttlMs`/`cacheScope`) -- and serves:

- `tools/list` and `tools/call` from the admin's selection, and
- one resource, the spec trimmed to the enabled operations.

Parsing is strict: duplicate keys are a parse error (two `name` keys must never mean
one thing to the hub and another to whatever reads the body next, ADR 0010 §3), and
batches are refused.

**Every tools/call goes through `dispatch_tool_call`.** That is the seam Epic 12's
`tool_permitted` check wraps (ADR 0010, ADR 0011 §10): a check placed anywhere else
would leave this backend uncovered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from mcp_hub.auth.caller import CallerIdentity
from mcp_hub.mcp.constants import (
    BACKWARD_COMPAT_PROTOCOL_VERSION,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    LEGACY_RESOURCE_NOT_FOUND,
    META_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    RESOURCE_NOT_FOUND,
    RESULT_TYPE_COMPLETE,
    STATELESS_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    UNSUPPORTED_PROTOCOL_VERSION,
)
from mcp_hub.models.server import RegisteredServer
from mcp_hub.openapi.executor import CallLimits, CallResult, ExchangeObserver, call_tool
from mcp_hub.openapi.model import MappingError, Tool
from mcp_hub.openapi.server_tools import cached_openapi_tools
from mcp_hub.openapi.trimmed_spec import trimmed_spec

logger = logging.getLogger(__name__)

_HANDSHAKE_VERSIONS = (PROTOCOL_VERSION, BACKWARD_COMPAT_PROTOCOL_VERSION)
# How long a stateless client may cache a list. Tools change only when an admin
# changes the selection or accepts a revision, so a minute is plenty fresh.
_LIST_TTL_MS = 60_000
_CAPABILITIES = {"tools": {"listChanged": False}, "resources": {}}
_MAX_VERSION_CHARS = 64
# Trimmed spec text by (spec hash, selection): trimming and serializing a large spec
# on every resources/read would make the resource a cheap way to load the hub.
_SPEC_CACHE_SIZE = 16
_spec_cache: OrderedDict[tuple[Any, ...], str] = OrderedDict()


@dataclass(frozen=True)
class VirtualReply:
    """The HTTP reply, plus what the proxy needs for the trace entry and span."""

    body: bytes
    status_code: int
    method: str = ""
    error: str = ""
    route: str = ""
    tool_name: str = ""
    needs_authentication: bool = False


@dataclass(frozen=True)
class CallContext:
    server: RegisteredServer
    caller: CallerIdentity
    description_limit: int
    limits: CallLimits
    http_client: httpx.AsyncClient | None = None
    on_exchange: ExchangeObserver | None = None


class _Duplicate(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _Duplicate(f"duplicate key {key!r}")
        result[key] = value
    return result


def _not_json(token: str) -> Any:
    raise ValueError(f"{token} is not a JSON number")


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):  # 1e999 parses as infinity without ever being NaN
        raise ValueError(f"{text} is not a finite number")
    return value


def _envelope(request_id: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> bytes:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    payload.update({"error": error} if error is not None else {"result": result})
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()


def _fail(status: int, request_id: Any, code: int, message: str, method: str = "") -> VirtualReply:
    body = _envelope(request_id, error={"code": code, "message": message})
    return VirtualReply(body, status, method=method, error=message)


def spec_uri(server: RegisteredServer) -> str:
    return f"openapi://{quote(server.id, safe='')}/spec"


async def handle_request(
    body: bytes, *, context: CallContext, protocol_header: str = ""
) -> VirtualReply:
    try:
        message = json.loads(
            body or b"null",
            object_pairs_hook=_no_duplicates,
            parse_constant=_not_json,
            parse_float=_finite,
        )
    except (ValueError, RecursionError) as exc:
        return _fail(400, None, PARSE_ERROR, f"invalid JSON: {exc}")
    if isinstance(message, list):
        return _fail(400, None, INVALID_REQUEST, "batch requests are not supported")
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        return _fail(400, None, INVALID_REQUEST, "expected a JSON-RPC request object")
    method: str = message["method"]
    params = message.get("params", {})
    if "id" not in message:
        return VirtualReply(b"", 202, method=method)  # a notification: nothing to say
    request_id = message["id"]
    if request_id is not None and (
        isinstance(request_id, bool) or not isinstance(request_id, (str, int))
    ):
        # JSON-RPC ids are strings, numbers or null; anything else is not echoed back.
        return _fail(200, None, INVALID_REQUEST, "id must be a string, an integer or null", method)
    if not isinstance(params, dict):
        return _fail(200, request_id, INVALID_PARAMS, "params must be an object", method)

    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    meta_version = meta.get(META_PROTOCOL_VERSION) if isinstance(meta, dict) else None
    if meta_version is not None and (
        not isinstance(meta_version, str) or meta_version not in SUPPORTED_PROTOCOL_VERSIONS
    ):
        return _fail(
            200,
            request_id,
            UNSUPPORTED_PROTOCOL_VERSION,
            f"unsupported protocol version {meta_version!r}",
            method,
        )
    stateless = STATELESS_PROTOCOL_VERSION in (protocol_header, meta_version)

    handler = _HANDLERS.get(method)
    if handler is None or (stateless and method == "initialize"):
        return _fail(200, request_id, METHOD_NOT_FOUND, f"method not found: {method}", method)
    try:
        return await handler(request_id, params, context, stateless)
    except Exception:
        # Logged for the operator; the caller gets a JSON-RPC error, not a 500 with
        # no trace entry and no span.
        logger.exception("OpenAPI server %s failed on %s", context.server.id, method)
        return _fail(200, request_id, INTERNAL_ERROR, "internal error", method)


def _ok(
    request_id: Any, method: str, result: dict[str, Any], stateless: bool, **extra: Any
) -> VirtualReply:
    if stateless:
        result = {**result, "resultType": RESULT_TYPE_COMPLETE}
    return VirtualReply(_envelope(request_id, result=result), 200, method=method, **extra)


def _server_info(server: RegisteredServer) -> dict[str, str]:
    info = server.openapi_snapshot.document.get("info", {}) if server.openapi_snapshot else {}
    spec_version = info.get("version") if isinstance(info.get("version"), str) else ""
    return {
        "name": server.name or server.id,
        # From the spec, so bounded: it goes back to every client that connects.
        "version": (server.version or spec_version or "0")[:_MAX_VERSION_CHARS],
    }


async def _initialize(
    request_id: Any, params: dict[str, Any], ctx: CallContext, stateless: bool
) -> VirtualReply:
    requested = params.get("protocolVersion")
    version = requested if requested in _HANDSHAKE_VERSIONS else PROTOCOL_VERSION
    result = {
        "protocolVersion": version,
        "capabilities": _CAPABILITIES,
        "serverInfo": _server_info(ctx.server),
    }
    return _ok(request_id, "initialize", result, stateless)


async def _discover(
    request_id: Any, params: dict[str, Any], ctx: CallContext, stateless: bool
) -> VirtualReply:
    versions = [STATELESS_PROTOCOL_VERSION, *_HANDSHAKE_VERSIONS]
    result = {
        "protocolVersions": versions,
        "capabilities": _CAPABILITIES,
        "serverInfo": _server_info(ctx.server),
    }
    return _ok(request_id, "server/discover", result, stateless)


async def _ping(
    request_id: Any, params: dict[str, Any], ctx: CallContext, stateless: bool
) -> VirtualReply:
    return _ok(request_id, "ping", {}, stateless)


async def _tools(ctx: CallContext) -> tuple[Tool, ...]:
    try:
        # Mapping a large spec takes most of a second: off the event loop.
        return await asyncio.to_thread(
            cached_openapi_tools, ctx.server, description_limit=ctx.description_limit
        )
    except MappingError as exc:
        logger.warning(
            "OpenAPI server %s has a selection that cannot be served: %s", ctx.server.id, exc
        )
        return ()


def _list(result: dict[str, Any], stateless: bool) -> dict[str, Any]:
    return {**result, "ttlMs": _LIST_TTL_MS, "cacheScope": "private"} if stateless else result


async def _tools_list(
    request_id: Any, params: dict[str, Any], ctx: CallContext, stateless: bool
) -> VirtualReply:
    tools = [tool.definition.to_mcp() for tool in await _tools(ctx)]
    return _ok(request_id, "tools/list", _list({"tools": tools}, stateless), stateless)


async def dispatch_tool_call(
    name: str, arguments: dict[str, Any], ctx: CallContext
) -> tuple[Tool | None, CallResult | None]:
    """**The one path every tools/call takes** for an OpenAPI server.

    Epic 12's `tool_permitted(policy, tool, principal, auth)` check wraps this
    function (ADR 0010, ADR 0011 §10). Returns ``(None, None)`` for a name that is
    not a selected tool, which the caller answers as "unknown tool": a hidden tool
    and a missing one must look the same.
    """
    tool = next((t for t in await _tools(ctx) if t.definition.name == name), None)
    if tool is None:
        return None, None
    result = await call_tool(
        tool,
        arguments,
        server=ctx.server,
        caller=ctx.caller,
        limits=ctx.limits,
        http_client=ctx.http_client,
        on_exchange=ctx.on_exchange,
    )
    return tool, result


async def _tools_call(
    request_id: Any, params: dict[str, Any], ctx: CallContext, stateless: bool
) -> VirtualReply:
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return _fail(
            200,
            request_id,
            INVALID_PARAMS,
            "tools/call needs a string name and object arguments",
            "tools/call",
        )
    tool, result = await dispatch_tool_call(name, arguments, ctx)
    if tool is None or result is None:
        return _fail(200, request_id, INVALID_PARAMS, f"Unknown tool: {name}", "tools/call")
    route = f"{tool.plan.method} {tool.plan.path}"
    if result.needs_authentication:
        return VirtualReply(
            b"Unauthorized",
            401,
            method="tools/call",
            error="authentication required",
            route=route,
            tool_name=name,
            needs_authentication=True,
        )
    error = "tool returned an error" if result.is_error else ""
    return _ok(
        request_id,
        "tools/call",
        result.to_mcp(),
        stateless,
        route=route,
        tool_name=name,
        error=error,
    )


async def _resources_list(
    request_id: Any, params: dict[str, Any], ctx: CallContext, stateless: bool
) -> VirtualReply:
    resource = {
        "uri": spec_uri(ctx.server),
        "name": "OpenAPI document",
        "description": "The API description, limited to the operations this server exposes.",
        "mimeType": "application/json",
    }
    return _ok(request_id, "resources/list", _list({"resources": [resource]}, stateless), stateless)


async def _resources_read(
    request_id: Any, params: dict[str, Any], ctx: CallContext, stateless: bool
) -> VirtualReply:
    uri = params.get("uri")
    snapshot = ctx.server.openapi_snapshot
    if uri != spec_uri(ctx.server) or snapshot is None:
        code = RESOURCE_NOT_FOUND if stateless else LEGACY_RESOURCE_NOT_FOUND
        return _fail(200, request_id, code, f"resource not found: {uri}", "resources/read")
    selection = frozenset(ref.key() for ref in ctx.server.openapi_selection)
    key = (snapshot.sha256, tuple(sorted((k.method, k.path) for k in selection)))
    text = _spec_cache.get(key)
    if text is None:
        document = await asyncio.to_thread(
            trimmed_spec, snapshot.document, snapshot.dialect, selection
        )
        text = json.dumps(document, ensure_ascii=False)
        _spec_cache[key] = text
        while len(_spec_cache) > _SPEC_CACHE_SIZE:
            _spec_cache.popitem(last=False)
    else:
        _spec_cache.move_to_end(key)
    content = {"uri": uri, "mimeType": "application/json", "text": text}
    return _ok(request_id, "resources/read", {"contents": [content]}, stateless)


_HANDLERS = {
    "initialize": _initialize,
    "server/discover": _discover,
    "ping": _ping,
    "tools/list": _tools_list,
    "tools/call": _tools_call,
    "resources/list": _resources_list,
    "resources/read": _resources_read,
}
