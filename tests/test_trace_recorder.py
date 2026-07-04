from datetime import datetime, timezone

import pytest
from devhub.trace.recorder import (
    redact_auth_header,
    sanitize_trace_headers,
    trim_trace_body,
)
from devhub.trace import TraceEntry, TraceRecorder


class TestRedactAuthHeader:
    def test_bearer_prefix(self) -> None:
        assert redact_auth_header("Bearer abc") == "Bearer ****"

    def test_bearer_prefix_case_insensitive(self) -> None:
        assert redact_auth_header("BEARER abc") == "Bearer ****"
        assert redact_auth_header("bEaReR abc") == "Bearer ****"

    def test_basic_prefix(self) -> None:
        assert redact_auth_header("Basic xyz") == "Basic ****"

    def test_basic_prefix_case_insensitive(self) -> None:
        assert redact_auth_header("BASIC xyz") == "Basic ****"

    def test_other_value(self) -> None:
        assert redact_auth_header("some-other-token") == "****"

    def test_empty_value(self) -> None:
        assert redact_auth_header("") == ""


class TestSanitizeTraceHeaders:
    def test_authorization_preserves_key(self) -> None:
        result = sanitize_trace_headers({"Authorization": "Bearer x"})
        assert "Authorization" in result

    def test_authorization_bearer_redacted(self) -> None:
        result = sanitize_trace_headers({"Authorization": "Bearer token123"})
        assert result["Authorization"] == "Bearer ****"

    def test_authorization_basic_redacted(self) -> None:
        result = sanitize_trace_headers({"Authorization": "Basic dXNlcjpwYXNz"})
        assert result["Authorization"] == "Basic ****"

    def test_authorization_other_redacted(self) -> None:
        result = sanitize_trace_headers({"Authorization": "Bearer xyz"})
        assert result["Authorization"] == "Bearer ****"

    def test_authorization_case_insensitive_key(self) -> None:
        result = sanitize_trace_headers({"authorization": "Bearer x"})
        assert result["authorization"] == "Bearer ****"

    def test_other_headers_unchanged(self) -> None:
        result = sanitize_trace_headers({"X-Other": "y", "Content-Type": "application/json"})
        assert result["X-Other"] == "y"
        assert result["Content-Type"] == "application/json"

    def test_mixed_headers(self) -> None:
        result = sanitize_trace_headers({"Authorization": "Bearer x", "X-Other": "y"})
        assert result == {"Authorization": "Bearer ****", "X-Other": "y"}


class TestTrimTraceBody:
    def test_truncation_needed(self) -> None:
        result = trim_trace_body("hello", body_limit=3)
        assert result == "hel\n…(truncated)…"

    def test_no_truncation_needed(self) -> None:
        result = trim_trace_body("hello", body_limit=10)
        assert result == "hello"

    def test_body_exactly_limit(self) -> None:
        result = trim_trace_body("hello", body_limit=5)
        assert result == "hello"

    def test_body_limit_zero(self) -> None:
        result = trim_trace_body("hello", body_limit=0)
        assert result == "hello"

    def test_body_limit_negative(self) -> None:
        result = trim_trace_body("hello", body_limit=-1)
        assert result == "hello"

    def test_empty_body(self) -> None:
        result = trim_trace_body("", body_limit=10)
        assert result == ""

    def test_body_shorter_than_limit(self) -> None:
        result = trim_trace_body("hi", body_limit=5)
        assert result == "hi"


class TestTraceEntry:
    def test_minimal_construction(self) -> None:
        timestamp = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        entry = TraceEntry(
            timestamp=timestamp,
            server_id="test-server",
            operation="proxy",
        )
        assert entry.timestamp == timestamp
        assert entry.server_id == "test-server"
        assert entry.operation == "proxy"

    def test_default_values_scalar_fields(self) -> None:
        entry = TraceEntry(
            timestamp=datetime.now(timezone.utc),
            server_id="test-server",
            operation="initialize",
        )
        assert entry.http_method == ""
        assert entry.url == ""
        assert entry.outbound_url == ""
        assert entry.status == 0
        assert entry.duration_ms == 0
        assert entry.error == ""
        assert entry.request_body == ""
        assert entry.response_body == ""

    def test_default_values_dict_fields(self) -> None:
        entry = TraceEntry(
            timestamp=datetime.now(timezone.utc),
            server_id="test-server",
            operation="tools/call",
        )
        assert entry.request_headers == {}
        assert entry.outbound_headers == {}
        assert entry.response_headers == {}

    def test_dict_fields_are_independent_empty_dicts(self) -> None:
        entry1 = TraceEntry(
            timestamp=datetime.now(timezone.utc),
            server_id="server-1",
            operation="health",
        )
        entry2 = TraceEntry(
            timestamp=datetime.now(timezone.utc),
            server_id="server-2",
            operation="proxy",
        )
        entry1.request_headers["X-Custom"] = "value"
        assert "X-Custom" not in entry2.request_headers

    def test_all_operation_values_accepted(self) -> None:
        timestamp = datetime.now(timezone.utc)
        for op in ("proxy", "initialize", "tools/call", "health"):
            entry = TraceEntry(
                timestamp=timestamp,
                server_id="test-server",
                operation=op,
            )
            assert entry.operation == op

    def test_custom_values_all_fields(self) -> None:
        timestamp = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        entry = TraceEntry(
            timestamp=timestamp,
            server_id="test-server",
            operation="proxy",
            http_method="GET",
            url="http://example.com/api",
            outbound_url="http://backend.example.com/api",
            status=200,
            duration_ms=150,
            error="",
            request_body='{"key": "value"}',
            response_body='{"result": "ok"}',
            request_headers={"Content-Type": "application/json"},
            outbound_headers={"Authorization": "Bearer token"},
            response_headers={"Content-Type": "application/json"},
        )
        assert entry.http_method == "GET"
        assert entry.url == "http://example.com/api"
        assert entry.outbound_url == "http://backend.example.com/api"
        assert entry.status == 200
        assert entry.duration_ms == 150
        assert entry.error == ""
        assert entry.request_body == '{"key": "value"}'
        assert entry.response_body == '{"result": "ok"}'
        assert entry.request_headers == {"Content-Type": "application/json"}
        assert entry.outbound_headers == {"Authorization": "Bearer token"}
        assert entry.response_headers == {"Content-Type": "application/json"}


class TestTraceRecorder:
    def make_entry(self, server_id: str, index: int) -> TraceEntry:
        return TraceEntry(
            timestamp=datetime(2024, 1, 1, 12, 0, 0, index * 1000, tzinfo=timezone.utc),
            server_id=server_id,
            operation="proxy",
        )

    def test_ring_buffer_eviction(self) -> None:
        recorder = TraceRecorder(limit=200)
        for i in range(250):
            recorder.add(self.make_entry("server-a", i))
        result = recorder.list("server-a")
        assert len(result) == 200
        assert result[0].timestamp.microsecond == 1000 * 50
        assert result[-1].timestamp.microsecond == 1000 * 249

    def test_ring_buffer_order_preserved(self) -> None:
        recorder = TraceRecorder(limit=200)
        for i in range(10):
            recorder.add(self.make_entry("server-b", i))
        result = recorder.list("server-b")
        assert len(result) == 10
        for i, entry in enumerate(result):
            assert entry.timestamp.microsecond == i * 1000

    def test_per_server_isolation(self) -> None:
        recorder = TraceRecorder(limit=200)
        for i in range(10):
            recorder.add(self.make_entry("server-x", i))
            recorder.add(self.make_entry("server-y", i + 10))
        result_x = recorder.list("server-x")
        result_y = recorder.list("server-y")
        assert len(result_x) == 10
        assert len(result_y) == 10
        assert result_x[0].timestamp.microsecond == 0
        assert result_y[0].timestamp.microsecond == 10000

    def test_clear_removes_only_target_server(self) -> None:
        recorder = TraceRecorder(limit=200)
        for i in range(5):
            recorder.add(self.make_entry("server-x", i))
            recorder.add(self.make_entry("server-y", i))
        recorder.clear("server-x")
        assert recorder.list("server-x") == []
        assert len(recorder.list("server-y")) == 5

    def test_empty_server_id_no_op(self) -> None:
        recorder = TraceRecorder(limit=200)
        recorder.add(self.make_entry("", 1))
        recorder.add(self.make_entry("server-a", 2))
        assert recorder.list("") == []
        assert len(recorder.list("server-a")) == 1

    def test_unknown_server_returns_empty_list(self) -> None:
        recorder = TraceRecorder(limit=200)
        result = recorder.list("nonexistent")
        assert result == []

    def test_list_returns_copy(self) -> None:
        recorder = TraceRecorder(limit=200)
        recorder.add(self.make_entry("server-a", 1))
        result = recorder.list("server-a")
        result.clear()
        result_2 = recorder.list("server-a")
        assert len(result_2) == 1
