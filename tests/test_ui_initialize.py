import pytest
from jinja2 import Environment, FileSystemLoader
from pathlib import Path


@pytest.fixture
def jinja_env() -> Environment:
    templates_dir = Path(__file__).parent.parent / "src" / "mcp_hub" / "templates"
    return Environment(loader=FileSystemLoader(str(templates_dir)))


@pytest.fixture
def initialize_template(jinja_env: Environment):
    return jinja_env.get_template("initialize.html")


class TestInitializeTemplate:
    def test_render_template_with_all_inputs(self, initialize_template) -> None:
        html = initialize_template.render(
            request_body='{"jsonrpc": "2.0", "method": "initialize", "params": {...}}',
            response_body='{"jsonrpc": "2.0", "result": {"protocolVersion": "2025-06-18"}}',
            protocol_version="2025-06-18",
            session_id="session-abc-123",
            duration_ms=45,
            auth_hint="Bearer scheme=Test",
        )

        assert "<pre" in html
        # Bodies are wrapped in the JSON block (Pretty/Raw toggle + copy), same as tool output.
        assert "data-tool-result" in html
        assert "data-format-toggle" in html
        request_count = html.count("<pre")
        assert request_count == 2

        assert "Protocol: 2025-06-18" in html
        assert "Session: session-abc-123" in html
        assert "45ms" in html

        assert "Auth Challenge" in html
        assert "Bearer scheme=Test" in html

    def test_render_template_without_auth_challenge(self, initialize_template) -> None:
        html = initialize_template.render(
            request_body='{"jsonrpc": "2.0", "method": "initialize"}',
            response_body='{"jsonrpc": "2.0", "result": {}}',
            protocol_version="2025-06-18",
            session_id="session-xyz",
            duration_ms=30,
        )

        assert "Auth Challenge" not in html
        assert "Bearer scheme=Test" not in html
        assert "amber" not in html.lower()

    def test_render_template_request_body_present(self, initialize_template) -> None:
        request_body = '{"method": "initialize", "id": 1}'
        escaped_body = request_body.replace('"', "&#34;")
        html = initialize_template.render(
            request_body=request_body,
            response_body='{"result": {"serverInfo": {}}}',
            protocol_version="2024-11-05",
            session_id="sess-001",
            duration_ms=100,
        )

        assert escaped_body in html

    def test_render_template_response_body_present(self, initialize_template) -> None:
        response_body = '{"result": {"protocolVersion": "2024-11-05", "capabilities": {}}}'
        escaped_body = response_body.replace('"', "&#34;")
        html = initialize_template.render(
            request_body='{"method": "initialize"}',
            response_body=response_body,
            protocol_version="2024-11-05",
            session_id="sess-002",
            duration_ms=50,
        )

        assert escaped_body in html

    def test_render_template_protocol_version_badge(self, initialize_template) -> None:
        html = initialize_template.render(
            request_body="{}",
            response_body="{}",
            protocol_version="2025-01-15",
            session_id="sess-003",
            duration_ms=25,
        )

        assert "Protocol: 2025-01-15" in html

    def test_render_template_session_id_badge(self, initialize_template) -> None:
        html = initialize_template.render(
            request_body="{}",
            response_body="{}",
            protocol_version="2025-01-15",
            session_id="unique-session-id-12345",
            duration_ms=75,
        )

        assert "Session: unique-session-id-12345" in html

    def test_render_template_duration_badge(self, initialize_template) -> None:
        html = initialize_template.render(
            request_body="{}",
            response_body="{}",
            protocol_version="2025-01-15",
            session_id="sess-005",
            duration_ms=150,
        )

        assert "150ms" in html

    def test_render_template_protocol_version_escaped(self, initialize_template) -> None:
        html = initialize_template.render(
            request_body="{}",
            response_body="{}",
            protocol_version="<script>alert('xss')</script>",
            session_id="sess-006",
            duration_ms=10,
        )

        assert "<script>" not in html
        assert "&#34;script&#34;>" in html or "&lt;script&gt;" in html

    def test_render_template_with_server_id_and_url(self, initialize_template) -> None:
        html = initialize_template.render(
            server_id="test-server-123",
            url="http://localhost:8080/mcp",
            request_body="{}",
            response_body="{}",
            protocol_version="2025-01-15",
            session_id="sess-007",
            duration_ms=50,
        )

        assert "test-server-123" in html
        assert "http://localhost:8080/mcp" in html
        assert "Server:" in html

    def test_render_template_with_error(self, initialize_template) -> None:
        html = initialize_template.render(
            request_body="{}",
            response_body="{}",
            protocol_version="2025-01-15",
            session_id="sess-008",
            duration_ms=50,
            error="Connection refused",
        )

        assert "Error" in html
        assert "Connection refused" in html

    def test_render_template_with_request_headers(self, initialize_template) -> None:
        request_headers = '{"Content-Type": "application/json", "Authorization": "Bearer ****"}'
        escaped_headers = request_headers.replace('"', "&#34;")
        html = initialize_template.render(
            request_body="{}",
            request_headers=request_headers,
            response_body="{}",
            protocol_version="2025-01-15",
            session_id="sess-009",
            duration_ms=50,
        )

        assert escaped_headers in html

    def test_render_template_with_response_status(self, initialize_template) -> None:
        html = initialize_template.render(
            request_body="{}",
            response_body="{}",
            protocol_version="2025-01-15",
            session_id="sess-010",
            duration_ms=50,
            response_status=200,
        )

        assert "Status: 200" in html

    def test_render_template_server_name_and_version(self, initialize_template) -> None:
        html = initialize_template.render(
            request_body="{}",
            response_body="{}",
            protocol_version="2025-01-15",
            session_id="sess-011",
            duration_ms=50,
            server_name="test-server",
            server_version="1.0.0",
        )

        assert "Server: test-server v1.0.0" in html


class TestInitializeRoute:
    def test_initialize_route_404_when_server_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from mcp_hub.app import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/ui/server/nonexistent-server/initialize")

        assert response.status_code == 404
