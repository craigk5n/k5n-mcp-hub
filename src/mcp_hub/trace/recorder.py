from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Mapping


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
        if key.lower() == "authorization":
            result[key] = redact_auth_header(value)
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
