from dataclasses import dataclass, field
from datetime import datetime, timezone

from mcp_hub.trace.recorder import TraceEntry, TraceRecorder


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Entry:
    timestamp: datetime
    server_id: str
    operation: str
    http_method: str
    url: str
    outbound_url: str
    status: int
    duration_ms: float
    error: str
    request_headers: dict[str, str] = field(default_factory=dict)
    response_headers: dict[str, str] = field(default_factory=dict)
    request_body: bytes = b""
    response_body: bytes = b""


def sanitize_headers(
    headers: dict[str, str],
) -> dict[str, str]:
    sanitized = dict(headers)
    auth_keys = [k for k in sanitized.keys() if k.lower() == "authorization"]
    for key in auth_keys:
        sanitized[key] = "[REDACTED]"
    return sanitized


def truncate_body(body: bytes, limit: int) -> bytes:
    if len(body) <= limit:
        return body
    return body[:limit] + b"...[truncated]"


def is_sse_content_type(content_type: str | None) -> bool:
    if content_type is None:
        return False
    return "text/event-stream" in content_type.lower()


__all__ = [
    "Entry",
    "TraceEntry",
    "TraceRecorder",
    "is_sse_content_type",
    "sanitize_headers",
    "truncate_body",
    "utcnow",
]
