from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Mapping


# Header names whose values may carry credentials or session material and must never be
# persisted in a captured trace. Compared case-insensitively. Note X-MCP-Token: this hub
# itself forwards bearer tokens in that header (see mcp.auth.apply_server_auth), so a trace
# would leak the raw token if it weren't redacted here.
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-mcp-token",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "x-access-token",
        "x-amz-security-token",
    }
)


def redact_auth_header(value: str) -> str:
    if not value:
        return ""
    lower_value = value.lower()
    if lower_value.startswith("bearer "):
        return "Bearer ****"
    if lower_value.startswith("basic "):
        return "Basic ****"
    return "****"


def sanitize_trace_headers(headers: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        lower_key = key.lower()
        if lower_key == "authorization":
            # Preserve the scheme (Bearer/Basic) for debuggability; hide the credential.
            result[key] = redact_auth_header(value)
        elif lower_key in SENSITIVE_HEADERS:
            result[key] = "****"
        else:
            result[key] = value
    return result


def trim_trace_body(body: str, *, body_limit: int) -> str:
    if body_limit <= 0 or len(body) <= body_limit:
        return body
    return body[:body_limit] + "\n…(truncated)…"


@dataclass
class TraceEntry:
    timestamp: datetime
    server_id: str
    operation: str
    http_method: str = ""
    url: str = ""
    outbound_url: str = ""
    status: int = 0
    duration_ms: int = 0
    error: str = ""
    request_body: str = ""
    response_body: str = ""
    request_headers: dict[str, str] = field(default_factory=dict)
    outbound_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)


class TraceRecorder:
    def __init__(self, limit: int = 200) -> None:
        self._limit = limit
        self._buffers: dict[str, deque[TraceEntry]] = {}
        self._lock = Lock()

    def add(self, entry: TraceEntry) -> None:
        if not entry.server_id:
            return
        with self._lock:
            if entry.server_id not in self._buffers:
                self._buffers[entry.server_id] = deque(maxlen=self._limit)
            self._buffers[entry.server_id].append(entry)

    def list(self, server_id: str) -> list[TraceEntry]:
        with self._lock:
            if server_id not in self._buffers:
                return []
            return list(self._buffers[server_id])

    def clear(self, server_id: str) -> None:
        with self._lock:
            if server_id in self._buffers:
                self._buffers[server_id].clear()
