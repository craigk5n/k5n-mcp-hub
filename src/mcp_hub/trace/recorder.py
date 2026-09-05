import dataclasses
import json
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any, Mapping, TypeVar


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
        # Grants access to an established MCP session on pre-2026-07-28 servers.
        "mcp-session-id",
    }
)


# Body fields that carry a credential. Deliberately an explicit OAuth-shaped list
# rather than anything matching "token": an MCP tool result may legitimately contain a
# field by that name, and over-redaction makes the trace useless for the debugging it
# exists to support.
SENSITIVE_BODY_FIELDS = frozenset(
    {
        "access_token",
        "refresh_token",
        "subject_token",
        "actor_token",
        "id_token",
        "client_secret",
        "client_assertion",
        "assertion",
        "password",
        "private_key",
    }
)

REDACTED = "****"

_FIELD_ALTERNATION = "|".join(sorted(SENSITIVE_BODY_FIELDS))
# Fallback for bodies that no longer parse. The proxy truncates before building the
# trace, so a mid-JSON cut is the common case, not an edge one.
_JSONISH = re.compile(rf'("(?:{_FIELD_ALTERNATION})"\s*:\s*")([^"]*)', re.IGNORECASE)
_FORMISH = re.compile(rf"\b({_FIELD_ALTERNATION})=([^&\s]+)", re.IGNORECASE)

BodyT = TypeVar("BodyT", str, bytes)


def _redact_structure(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                REDACTED if str(key).lower() in SENSITIVE_BODY_FIELDS else _redact_structure(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_structure(item) for item in value]
    return value


def _redact_text(text: str) -> str:
    text = _JSONISH.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    return _FORMISH.sub(lambda m: f"{m.group(1)}={REDACTED}", text)


def sanitize_trace_body(body: BodyT) -> BodyT:
    """Redact credential-bearing fields inside a captured request/response body.

    Handles JSON and form-encoded payloads structurally, and falls back to a textual
    pass for anything that no longer parses — which includes every truncated body.
    Returns the same type it was given, and unparseable binary unchanged.
    """
    if not body:
        return body

    if isinstance(body, bytes):
        try:
            decoded = body.decode("utf-8")
        except UnicodeDecodeError:
            return body
        return _sanitize_text(decoded).encode("utf-8")  # type: ignore[return-value]

    return _sanitize_text(body)  # type: ignore[return-value]


def _sanitize_text(text: str) -> str:
    try:
        parsed = json.loads(text)
    except ValueError:
        return _redact_text(text)

    if isinstance(parsed, (dict, list)):
        return json.dumps(_redact_structure(parsed))
    return _redact_text(text)


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


def format_headers(headers: Mapping[str, str]) -> str:
    """Render headers as newline-separated ``Name: Value`` lines (the real HTTP wire
    format) rather than a JSON object or Python dict repr, which misrepresent how headers
    are actually sent/received."""
    return "\n".join(f"{key}: {value}" for key, value in headers.items())


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
        # Redact here rather than at each call site: this is the one place every
        # traced body passes through, and both Entry shapes reach it.
        entry = dataclasses.replace(
            entry,
            request_body=sanitize_trace_body(entry.request_body),
            response_body=sanitize_trace_body(entry.response_body),
        )
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
