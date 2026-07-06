import asyncio
import json
from typing import Any

from fastapi import Response
from starlette.datastructures import Headers

from mcp_hub.models.server import RegisteredServer

DEFAULT_FAULT_TIMEOUT_MS = 5000


def _get_header(headers: dict[str, str] | Headers, key: str) -> str | None:
    lower_key = key.lower()
    if isinstance(headers, Headers):
        return headers.get(lower_key)
    for k, v in headers.items():
        if k.lower() == lower_key:
            return v
    return None


async def apply_fault_injection(
    request: dict[str, Any],
    server: RegisteredServer,
) -> Response | None:
    fault_injection = server.fault_injection

    if not fault_injection.enabled:
        return None

    headers = request.get("headers", {})

    if fault_injection.sse_interrupt:
        accept = _get_header(headers, "Accept") or ""
        method = request.get("method", "")
        if "text/event-stream" in accept or method == "GET":
            return Response(
                content=b'event: error\ndata: {"error":"sse interrupted"}\n\n',
                status_code=200,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )

    if fault_injection.timeout_enabled:
        wait_ms = fault_injection.timeout_millis or DEFAULT_FAULT_TIMEOUT_MS
        if not isinstance(wait_ms, (int, float)) or wait_ms < 0:
            wait_ms = DEFAULT_FAULT_TIMEOUT_MS
        await asyncio.sleep(wait_ms / 1000)
        return Response(
            content=b"Injected timeout\n",
            status_code=504,
            media_type="text/plain",
        )

    if fault_injection.malformed_json:
        return Response(
            content=b"{bad json",
            status_code=200,
            media_type="application/json",
        )

    if fault_injection.invalid_method:
        body = request.get("body", {})
        request_id: Any = None
        try:
            if isinstance(body, dict):
                request_id = body.get("id")
            elif isinstance(body, str):
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        request_id = parsed.get("id")
                except json.JSONDecodeError:
                    pass
        except (json.JSONDecodeError, AttributeError):
            pass

        error_response = {
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": "Method not found"},
            "id": request_id,
        }
        return Response(
            content=json.dumps(error_response).encode(),
            status_code=200,
            media_type="application/json",
        )

    return None
