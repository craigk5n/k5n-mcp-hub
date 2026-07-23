from __future__ import annotations

import pytest
from jinja2 import Environment, FileSystemLoader

from mcp_hub.trace import sanitize_headers, truncate_body
from mcp_hub.trace.recorder import format_headers, sanitize_trace_headers
from mcp_hub.utils import dom_id


@pytest.fixture
def jinja_env() -> Environment:
    templates_dir = "src/mcp_hub/templates"
    env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=True,
    )
    env.filters["sanitize_headers"] = sanitize_trace_headers
    env.filters["format_headers"] = format_headers
    env.filters["dom_id"] = dom_id
    return env


@pytest.fixture
def sample_entries() -> list[dict]:
    return [
        {
            "timestamp": "2026-07-03T10:15:30.123456+00:00",
            "operation": "proxy",
            "http_method": "GET",
            "url": "/mcp/v1/tools/list",
            "status": 200,
            "duration_ms": 45.67,
            "error": "",
            "request_headers": {
                "Authorization": "Bearer test123",
                "Content-Type": "application/json",
            },
            "response_headers": {"Content-Type": "application/json"},
            "request_body": '{"jsonrpc": "2.0"}',
            "response_body": '{"result": {"tools": []}}',
        },
        {
            "timestamp": "2026-07-03T10:15:25.123456+00:00",
            "operation": "health_check",
            "http_method": "GET",
            "url": "/health",
            "status": 404,
            "duration_ms": 12.34,
            "error": "Not Found",
            "request_headers": {},
            "response_headers": {},
            "request_body": "",
            "response_body": "",
        },
    ]


class TestTraceTemplate:
    def test_renders_all_entry_fields(
        self, jinja_env: Environment, sample_entries: list[dict]
    ) -> None:
        template = jinja_env.get_template("trace.html")
        server_id = "test-server"

        html = template.render(
            server_id=server_id,
            entries=sample_entries,
            verbose=False,
        )

        for entry in sample_entries:
            assert entry["timestamp"] in html, "timestamp should be present"
            assert entry["operation"] in html, "operation should be present"
            assert entry["http_method"] in html, "http_method should be present"
            assert entry["url"] in html, "url should be present"
            assert str(entry["status"]) in html, "status should be present"
            assert f"{entry['duration_ms']:.2f}ms" in html, "duration should be present"
            assert entry["error"] in html, "error should be present"

    def test_all_three_toggles_present(
        self, jinja_env: Environment, sample_entries: list[dict]
    ) -> None:
        template = jinja_env.get_template("trace.html")

        html = template.render(
            server_id="test-server",
            entries=sample_entries,
            verbose=False,
        )

        assert "Show health checks" in html, "Show health checks toggle should be present"
        assert "Format JSON" in html, "Format JSON toggle should be present"
        assert "Newest first" in html, "Newest first toggle should be present"

    def test_all_four_buttons_present(
        self, jinja_env: Environment, sample_entries: list[dict]
    ) -> None:
        template = jinja_env.get_template("trace.html")

        html = template.render(
            server_id="test-server",
            entries=sample_entries,
            verbose=False,
        )

        assert "Enable Verbose" in html or "Disable Verbose" in html, (
            "Verbose toggle button should be present"
        )
        assert "Refresh" in html, "Refresh button should be present"
        assert "Clear" in html, "Clear button should be present"
        assert "Collapse" in html, "Collapse button should be present"

    def test_verbose_mode_shows_details(
        self, jinja_env: Environment, sample_entries: list[dict]
    ) -> None:
        template = jinja_env.get_template("trace.html")

        html = template.render(
            server_id="test-server",
            entries=sample_entries,
            verbose=True,
        )

        assert "Request Headers" in html, (
            "Request headers section should be present in verbose mode"
        )
        assert "Response Headers" in html, (
            "Response headers section should be present in verbose mode"
        )
        assert "Request Body" in html, "Request body section should be present in verbose mode"
        assert "Response Body" in html, "Response body section should be present in verbose mode"
        assert "View Details" in html, "View Details toggle should be present in verbose mode"

    def test_non_verbose_mode_hides_details(
        self, jinja_env: Environment, sample_entries: list[dict]
    ) -> None:
        template = jinja_env.get_template("trace.html")

        html = template.render(
            server_id="test-server",
            entries=sample_entries,
            verbose=False,
        )

        assert "Request Headers" not in html, (
            "Request headers section should NOT be present when not verbose"
        )
        assert "Response Headers" not in html, (
            "Response headers section should NOT be present when not verbose"
        )
        assert "Request Body" not in html, (
            "Request body section should NOT be present when not verbose"
        )
        assert "Response Body" not in html, (
            "Response body section should NOT be present when not verbose"
        )
        assert "View Details" not in html, "View Details should NOT be present when not verbose"

    def test_disable_verbose_button_when_verbose_true(
        self, jinja_env: Environment, sample_entries: list[dict]
    ) -> None:
        template = jinja_env.get_template("trace.html")

        html = template.render(
            server_id="test-server",
            entries=sample_entries,
            verbose=True,
        )

        assert "Disable Verbose" in html, "Should show Disable Verbose when verbose is true"

    def test_enable_verbose_button_when_verbose_false(
        self, jinja_env: Environment, sample_entries: list[dict]
    ) -> None:
        template = jinja_env.get_template("trace.html")

        html = template.render(
            server_id="test-server",
            entries=sample_entries,
            verbose=False,
        )

        assert "Enable Verbose" in html, "Should show Enable Verbose when verbose is false"

    def test_server_id_in_template(self, jinja_env: Environment) -> None:
        template = jinja_env.get_template("trace.html")
        server_id = "my-custom-server"

        html = template.render(
            server_id=server_id,
            entries=[],
            verbose=False,
        )

        assert server_id in html, "server_id should be in the template"


class TestTraceHelpers:
    def test_sanitize_headers_redacts_authorization(self) -> None:
        headers = {"Authorization": "Bearer secret123", "Content-Type": "application/json"}
        result = sanitize_headers(headers)

        assert result["Authorization"] == "[REDACTED]"
        assert result["Content-Type"] == "application/json"

    def test_sanitize_headers_case_insensitive(self) -> None:
        headers = {"authorization": "Bearer secret123", "Content-Type": "application/json"}
        result = sanitize_headers(headers)

        assert result["authorization"] == "[REDACTED]"

    def test_sanitize_headers_no_auth_header(self) -> None:
        headers = {"Content-Type": "application/json", "X-Custom": "value"}
        result = sanitize_headers(headers)

        assert result == headers

    def test_truncate_body_under_limit(self) -> None:
        body = b"short body"
        result = truncate_body(body, 100)

        assert result == body

    def test_truncate_body_over_limit(self) -> None:
        body = b"a" * 200
        result = truncate_body(body, 100)

        assert result == b"a" * 100 + b"...[truncated]"
        assert len(result) == 100 + len(b"...[truncated]")

    def test_truncate_body_at_limit(self) -> None:
        body = b"a" * 100
        result = truncate_body(body, 100)

        assert result == body
