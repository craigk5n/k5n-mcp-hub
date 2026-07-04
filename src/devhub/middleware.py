from __future__ import annotations

import logging
import secrets
import time
from typing import Callable, TYPE_CHECKING

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from devhub.metrics import Metrics

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


class RequestIdMetricsMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI, metrics: Metrics) -> None:  # type: ignore[valid-type]
        super().__init__(app)
        self._metrics = metrics

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID")
        if not request_id:
            request_id = secrets.token_hex(16)

        request.state.request_id = request_id

        self._metrics.inc_in_flight()
        self._metrics.inc_requests_total()

        start_time = time.perf_counter()
        status_code = 200
        error_counted = False

        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception:
            status_code = 500
            self._metrics.inc_errors_total()
            error_counted = True
            raise
        finally:
            end_time = time.perf_counter()
            duration_ms = (end_time - start_time) * 1000
            self._metrics.add_duration_ms_sum(duration_ms)
            self._metrics.dec_in_flight()

            if status_code >= 500 and not error_counted:
                self._metrics.inc_errors_total()

            logger.debug(
                "request_complete",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": status_code,
                    "duration_ms": round(duration_ms, 2),
                    "request_id": request_id,
                },
            )


def create_request_id_metrics_middleware(metrics: Metrics) -> type[RequestIdMetricsMiddleware]:
    class ConfiguredMiddleware(RequestIdMetricsMiddleware):
        def __init__(self, app: FastAPI) -> None:  # type: ignore[valid-type]
            super().__init__(app, metrics)

    return ConfiguredMiddleware
