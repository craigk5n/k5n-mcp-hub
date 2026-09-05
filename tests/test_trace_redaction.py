"""Trace capture must not persist credentials or session material beyond Authorization.

This hub forwards bearer tokens in X-MCP-Token (see mcp.auth.apply_server_auth), so a
trace that only redacted Authorization would leak the raw token. These tests pin the
broadened redaction in both sanitizers.
"""

from mcp_hub.trace import SENSITIVE_HEADERS, sanitize_headers
from mcp_hub.trace.recorder import sanitize_trace_headers


def test_sanitize_trace_headers_preserves_authorization_scheme() -> None:
    result = sanitize_trace_headers({"Authorization": "Bearer super-secret-token"})
    assert result["Authorization"] == "Bearer ****"


def test_sanitize_trace_headers_redacts_x_mcp_token() -> None:
    # X-MCP-Token carries the raw bearer token this hub forwards; it must be hidden.
    result = sanitize_trace_headers({"X-MCP-Token": "super-secret-token"})
    assert "super-secret-token" not in result["X-MCP-Token"]
    assert result["X-MCP-Token"] == "****"


def test_sanitize_trace_headers_redacts_cookies_and_api_keys() -> None:
    headers = {
        "Cookie": "session=abc123",
        "Set-Cookie": "session=abc123; HttpOnly",
        "X-Api-Key": "key-123",
        "Api-Key": "key-456",
        "X-Auth-Token": "tok-789",
    }
    result = sanitize_trace_headers(headers)
    for key, original in headers.items():
        assert original.split("=")[-1] not in result[key]
        assert result[key] == "****"


def test_sanitize_trace_headers_is_case_insensitive() -> None:
    result = sanitize_trace_headers({"x-mcp-token": "secret", "COOKIE": "s=1"})
    assert result["x-mcp-token"] == "****"
    assert result["COOKIE"] == "****"


def test_sanitize_trace_headers_passes_through_benign_headers() -> None:
    result = sanitize_trace_headers(
        {"Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}
    )
    assert result["Content-Type"] == "application/json"
    assert result["MCP-Protocol-Version"] == "2025-06-18"


def test_sanitize_headers_redacts_full_sensitive_set() -> None:
    headers = {
        "Authorization": "Bearer secret",
        "X-MCP-Token": "secret",
        "Cookie": "session=abc",
        "Content-Type": "application/json",
    }
    result = sanitize_headers(headers)
    assert result["Authorization"] == "[REDACTED]"
    assert result["X-MCP-Token"] == "[REDACTED]"
    assert result["Cookie"] == "[REDACTED]"
    assert result["Content-Type"] == "application/json"


def test_sensitive_headers_set_is_lowercase() -> None:
    # Lookups compare header.lower() against the set, so every entry must be lowercase.
    assert all(name == name.lower() for name in SENSITIVE_HEADERS)
    assert "x-mcp-token" in SENSITIVE_HEADERS
    assert "authorization" in SENSITIVE_HEADERS


def test_sanitize_trace_headers_redacts_mcp_session_id() -> None:
    # Session ids grant access to an established MCP session on legacy servers —
    # treat them like any other credential in traces.
    result = sanitize_trace_headers({"Mcp-Session-Id": "sess-abc-123"})
    assert result["Mcp-Session-Id"] == "****"


def test_sanitize_headers_redacts_mcp_session_id() -> None:
    result = sanitize_headers({"MCP-SESSION-ID": "sess-abc-123"})
    assert result["MCP-SESSION-ID"] == "[REDACTED]"


def test_mcp_session_id_in_sensitive_set() -> None:
    assert "mcp-session-id" in SENSITIVE_HEADERS


def test_identity_assertion_header_is_redacted() -> None:
    """Story 8.3: the EMA identity assertion is a credential like any other."""
    from mcp_hub.trace.recorder import sanitize_trace_headers

    result = sanitize_trace_headers({"X-MCP-Identity-Assertion": "header.payload.signature"})

    assert result["X-MCP-Identity-Assertion"] == "****"
