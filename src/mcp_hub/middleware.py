from __future__ import annotations

import logging
import secrets
import time
from typing import TYPE_CHECKING

from starlette.datastructures import MutableHeaders

from mcp_hub.metrics import Metrics

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class RequestIdMetricsMiddleware:
    """Pure-ASGI request-ID + metrics middleware.

    Implemented at the raw ASGI layer rather than Starlette's BaseHTTPMiddleware:
    BaseHTTPMiddleware wraps the receive channel in a task group, which deadlocks /
    raises "No response returned" + cancel-scope errors when a downstream endpoint reads
    the request body (``await request.body()``) — as the /v1/register handler does. A
    plain ASGI middleware only wraps ``send``, so body reads work normally.
    """

    def __init__(self, app: "ASGIApp", metrics: Metrics) -> None:
        self.app = app
        self._metrics = metrics

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        request_id = headers.get("x-request-id") or secrets.token_hex(16)
        # expose as request.state.request_id for downstream handlers
        scope.setdefault("state", {})["request_id"] = request_id

        self._metrics.inc_in_flight()
        self._metrics.inc_requests_total()
        start_time = time.perf_counter()
        status_code = 500
        error_counted = False

        async def send_wrapper(message: "Message") -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                hdrs = MutableHeaders(scope=message)
                hdrs["X-Request-ID"] = request_id
                # Never let browsers cache the admin HTML — otherwise iOS Safari (which
                # caches aggressively and ignores a plain reload) keeps serving a stale
                # page after an update. Static assets under /static still cache via ETag.
                if "text/html" in hdrs.get("content-type", ""):
                    hdrs["Cache-Control"] = "no-store, must-revalidate"
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            self._metrics.inc_errors_total()
            error_counted = True
            raise
        finally:
            duration_ms = (time.perf_counter() - start_time) * 1000
            self._metrics.add_duration_ms_sum(duration_ms)
            self._metrics.dec_in_flight()
            if status_code >= 500 and not error_counted:
                self._metrics.inc_errors_total()
            logger.debug(
                "request_complete",
                extra={
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status_code,
                    "duration_ms": round(duration_ms, 2),
                    "request_id": request_id,
                },
            )


def create_request_id_metrics_middleware(metrics: Metrics) -> type[RequestIdMetricsMiddleware]:
    class ConfiguredMiddleware(RequestIdMetricsMiddleware):
        def __init__(self, app: "ASGIApp") -> None:
            super().__init__(app, metrics)

    return ConfiguredMiddleware
