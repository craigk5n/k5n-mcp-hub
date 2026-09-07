"""Forward one JSON-RPC request to a stdio server and shape the reply.

A client sends the hub exactly what it would send any MCP server; whether the
backend is a URL or a subprocess is the hub's problem. So this returns the same
JSON-RPC envelope the HTTP path returns, including for failures — a call that
reaches the server and is refused by it is the server's answer, not a gateway error,
and answering 502 would tell the client the wrong thing about what happened.

There is no streaming here. A stdio session is request/response over one pipe pair,
so the SSE tee that `proxy/handler.py` performs for HTTP has nothing to tee.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# JSON-RPC reserved codes (https://www.jsonrpc.org/specification#error_object).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INTERNAL_ERROR = -32603


class StdioProxyResult:
    """The response body and status to hand back to the caller."""

    def __init__(self, body: bytes, status_code: int, error: str = "") -> None:
        self.body = body
        self.status_code = status_code
        # Recorded on the trace entry; empty when the exchange itself succeeded, even
        # if the server answered with a JSON-RPC error.
        self.error = error


def _envelope(request_id: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> bytes:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return json.dumps(payload).encode()


async def forward_to_stdio(
    *,
    pool: Any,
    server: Any,
    request_body: bytes,
    timeout: float = 30.0,
) -> StdioProxyResult:
    try:
        parsed = json.loads(request_body or b"{}")
    except json.JSONDecodeError as e:
        return StdioProxyResult(
            _envelope(None, error={"code": PARSE_ERROR, "message": f"invalid json: {e}"}),
            400,
            error=str(e),
        )

    if not isinstance(parsed, dict):
        return StdioProxyResult(
            _envelope(None, error={"code": INVALID_REQUEST, "message": "expected a JSON object"}),
            400,
            error="expected a JSON object",
        )

    method = parsed.get("method")
    request_id = parsed.get("id")
    params = parsed.get("params")

    if not isinstance(method, str) or not method:
        return StdioProxyResult(
            _envelope(request_id, error={"code": INVALID_REQUEST, "message": "missing method"}),
            400,
            error="missing method",
        )

    if pool is None:
        return StdioProxyResult(
            _envelope(
                request_id,
                error={"code": INTERNAL_ERROR, "message": "stdio servers are not enabled"},
            ),
            503,
            error="stdio servers are not enabled",
        )

    try:
        session = await pool.session(server)
    except Exception as e:
        # The server could not be started. Unlike a refusal *from* a server, this is a
        # gateway-side failure, so it gets 502 the way an unreachable HTTP backend does.
        logger.warning("stdio proxy could not start %s: %s", server.id, e)
        return StdioProxyResult(
            _envelope(request_id, error={"code": INTERNAL_ERROR, "message": str(e)}),
            502,
            error=str(e),
        )

    # A notification (no id) expects no reply, and the client is not waiting for one.
    if request_id is None:
        try:
            await session.send_notification_raw(method, params)  # type: ignore[attr-defined]
        except AttributeError:
            # The SDK exposes typed notification senders rather than a raw one; the
            # hub does not proxy notifications to stdio servers today.
            logger.info("dropping unsupported stdio notification %r for %s", method, server.id)
        except Exception as e:
            logger.warning("stdio notification %r failed for %s: %s", method, server.id, e)
        return StdioProxyResult(b"", 202)

    dispatcher = getattr(session, "_dispatcher", None)
    if dispatcher is None or not hasattr(dispatcher, "send_raw_request"):
        return StdioProxyResult(
            _envelope(
                request_id,
                error={"code": INTERNAL_ERROR, "message": "stdio transport is unavailable"},
            ),
            502,
            error="stdio transport is unavailable",
        )

    try:
        # The dispatcher, not a typed `session.call_tool`: the hub forwards whatever
        # method the client sent, including ones this SDK version has no wrapper for,
        # and returns the result unvalidated. Validating here would mean rejecting
        # responses the client might have handled perfectly well — the same reason
        # `sdk_client._list_leniently` drops to this layer.
        raw = await dispatcher.send_raw_request(method, params, {"timeout": timeout})
    except Exception as e:
        message = str(e)
        logger.info("stdio call %r on %s failed: %s", method, server.id, message)
        # The server answered, and its answer was an error. That is a JSON-RPC error
        # for the client to read, not a gateway failure.
        return StdioProxyResult(
            _envelope(request_id, error={"code": INTERNAL_ERROR, "message": message}),
            200,
            error=message,
        )

    return StdioProxyResult(_envelope(request_id, result=raw), 200)
