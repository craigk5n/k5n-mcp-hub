import pytest
import pytest_asyncio
import re
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.models import RegisteredServer


from typing import Literal


def create_test_server(
    server_id: str,
    healthy: bool = False,
    rate_limited: bool = False,
    consecutive_fails: int = 0,
    last_checked: datetime | None = None,
    uptime_seconds: float = 0.0,
    mcp_transport: Literal["http", "sse", ""] = "",
    mcp_protocol_version: str = "",
    mcp_conformant: bool | None = None,
    auth_type: Literal["bearer", "oauth", ""] = "",
    oauth_token_status: Literal["ok", "error", ""] = "",
    schema_conformant: bool | None = None,
    schema_issues: list[str] | None = None,
) -> RegisteredServer:
    return RegisteredServer(
        id=server_id,
        url=f"http://localhost:8000/{server_id}",
        name=f"Test Server {server_id}",
        healthy=healthy,
        rate_limited=rate_limited,
        consecutive_fails=consecutive_fails,
        last_checked=last_checked,
        uptime_seconds=uptime_seconds,
        mcp_transport=mcp_transport,
        mcp_protocol_version=mcp_protocol_version,
        mcp_conformant=mcp_conformant,
        auth_type=auth_type,
        oauth_token_status=oauth_token_status,
        schema_conformant=schema_conformant,
        schema_issues=schema_issues or [],
    )


class TestHealthBadge:
    @pytest.fixture
    def app(self):
        return create_app()

    @pytest.fixture
    def client(self, app):
        return TestClient(app, raise_server_exceptions=False)

    @pytest_asyncio.fixture
    async def healthy_server(self, app):
        server = create_test_server(
            server_id="healthy-server",
            healthy=True,
            consecutive_fails=0,
            last_checked=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            uptime_seconds=3600,
            mcp_transport="sse",
            mcp_protocol_version="2024-11-05",
            mcp_conformant=True,
        )
        await app.state.registry.register(server)
        return server

    @pytest_asyncio.fixture
    async def unhealthy_server(self, app):
        server = create_test_server(
            server_id="unhealthy-server",
            healthy=False,
            consecutive_fails=3,
            last_checked=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            uptime_seconds=0,
            mcp_transport="http",
            mcp_protocol_version="2024-11-05",
            mcp_conformant=False,
        )
        await app.state.registry.register(server)
        return server

    @pytest_asyncio.fixture
    async def unknown_status_server(self, app):
        server = create_test_server(
            server_id="unknown-server",
            healthy=False,
            last_checked=None,
            uptime_seconds=0,
        )
        await app.state.registry.register(server)
        return server

    @pytest_asyncio.fixture
    async def oauth_server(self, app):
        server = create_test_server(
            server_id="oauth-server",
            healthy=True,
            auth_type="oauth",
            oauth_token_status="ok",
        )
        await app.state.registry.register(server)
        return server

    @pytest_asyncio.fixture
    async def non_oauth_server(self, app):
        server = create_test_server(
            server_id="non-oauth-server",
            healthy=True,
            auth_type="bearer",
            oauth_token_status="",
        )
        await app.state.registry.register(server)
        return server

    @pytest_asyncio.fixture
    async def full_server(self, app):
        server = create_test_server(
            server_id="full-server",
            healthy=True,
            consecutive_fails=0,
            last_checked=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            uptime_seconds=7200,
            mcp_transport="sse",
            mcp_protocol_version="2024-11-05",
            mcp_conformant=True,
            auth_type="oauth",
            oauth_token_status="ok",
            schema_conformant=True,
            schema_issues=[],
        )
        await app.state.registry.register(server)
        return server

    @pytest_asyncio.fixture
    async def schema_invalid_server(self, app):
        server = create_test_server(
            server_id="schema-invalid-server",
            healthy=True,
            last_checked=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            mcp_protocol_version="2024-11-05",
            schema_conformant=False,
            schema_issues=["Invalid tool name", "Missing description"],
        )
        await app.state.registry.register(server)
        return server

    def test_healthy_server_returns_html_containing_healthy(
        self, client: TestClient, healthy_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{healthy_server.id}/health-status")

        assert response.status_code == 200
        assert "Healthy" in response.text

    def test_unknown_server_returns_404(self, client: TestClient) -> None:
        response = client.get("/api/servers/nonexistent-server/health-status")

        assert response.status_code == 404

    def test_content_type_is_text_html(
        self, client: TestClient, healthy_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{healthy_server.id}/health-status")

        assert response.status_code == 200
        assert response.headers.get("content-type") == "text/html; charset=utf-8"

    def test_oauth_badge_not_included_when_not_applicable(
        self, client: TestClient, non_oauth_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{non_oauth_server.id}/health-status")

        assert response.status_code == 200
        assert "OAuth token" not in response.text

    def test_oauth_badge_included_when_auth_type_oauth(
        self, client: TestClient, oauth_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{oauth_server.id}/health-status")

        assert response.status_code == 200
        assert "OAuth token" in response.text

    def test_healthy_badge_has_green_healthy_styling(
        self, client: TestClient, healthy_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{healthy_server.id}/health-status")

        assert response.status_code == 200
        assert "bg-green-100" in response.text
        assert "text-green-800" in response.text
        assert "border-green-200" in response.text

    def test_unhealthy_server_shows_unhealthy_with_fails(
        self, client: TestClient, unhealthy_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{unhealthy_server.id}/health-status")

        assert response.status_code == 200
        assert "Unhealthy" in response.text
        assert "3 fails" in response.text
        assert "bg-red-100" in response.text

    def test_unknown_status_shows_unknown(
        self, client: TestClient, unknown_status_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{unknown_status_server.id}/health-status")

        assert response.status_code == 200
        assert "Unknown" in response.text
        assert "bg-slate-100" in response.text

    @pytest_asyncio.fixture
    async def rate_limited_server(self, app):
        server = create_test_server(
            server_id="rate-limited-server",
            healthy=True,
            rate_limited=True,
            last_checked=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            uptime_seconds=3600,
        )
        await app.state.registry.register(server)
        return server

    def test_rate_limited_shows_degraded_amber_not_green(
        self, client: TestClient, rate_limited_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{rate_limited_server.id}/health-status")

        assert response.status_code == 200
        # Distinct "Rate-limited" state in amber, not the green "Healthy" badge...
        assert "Rate-limited" in response.text
        assert "bg-amber-100" in response.text
        assert "✓ Healthy" not in response.text
        # ...and the green uptime badge is suppressed for a degraded server.
        assert "Uptime: 1h" not in response.text

    def test_badge_order_health_then_uptime_then_transport(
        self, client: TestClient, healthy_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{healthy_server.id}/health-status")
        html = response.text

        healthy_pos = html.find("Healthy")
        uptime_pos = html.find("Uptime:")
        transport_pos = html.find("Transport:")

        assert healthy_pos != -1, "Health badge not found"
        assert uptime_pos != -1, "Uptime badge not found"
        assert transport_pos != -1, "Transport badge not found"
        assert healthy_pos < uptime_pos < transport_pos, "Badges not in correct order"

    def test_transport_badge_sse_uses_sky_classes(
        self, client: TestClient, healthy_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{healthy_server.id}/health-status")

        assert response.status_code == 200
        assert "Transport: Streamable HTTP" in response.text
        assert "bg-sky-100" in response.text
        assert "text-sky-800" in response.text
        assert "border-sky-200" in response.text

    def test_transport_badge_http_uses_slate_classes(
        self, client: TestClient, unhealthy_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{unhealthy_server.id}/health-status")

        assert response.status_code == 200
        assert "Transport: HTTP" in response.text
        assert "bg-slate-100" in response.text

    @pytest_asyncio.fixture
    async def supported_version_server(self, app):
        server = create_test_server(
            server_id="supported-version-server",
            healthy=True,
            last_checked=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            mcp_protocol_version="2025-11-25",
            mcp_conformant=True,
        )
        await app.state.registry.register(server)
        return server

    def test_mcp_protocol_version_supported(
        self, client: TestClient, supported_version_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{supported_version_server.id}/health-status")

        assert response.status_code == 200
        assert "MCP 2025-11-25 - supported" in response.text
        assert "bg-emerald-100" in response.text
        assert "text-emerald-800" in response.text

    @pytest_asyncio.fixture
    async def backward_compat_version_server(self, app):
        server = create_test_server(
            server_id="backward-compat-server",
            healthy=True,
            last_checked=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            mcp_protocol_version="2025-06-18",
            mcp_conformant=True,
        )
        await app.state.registry.register(server)
        return server

    def test_mcp_protocol_version_supported_backward_compat(
        self, client: TestClient, backward_compat_version_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{backward_compat_version_server.id}/health-status")

        assert response.status_code == 200
        assert "MCP 2025-06-18 - supported" in response.text
        assert "bg-emerald-100" in response.text

    @pytest_asyncio.fixture
    async def stateless_version_server(self, app):
        server = create_test_server(
            server_id="stateless-version-server",
            healthy=True,
            last_checked=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            mcp_protocol_version="2026-07-28",
            mcp_conformant=True,
        )
        await app.state.registry.register(server)
        return server

    def test_mcp_protocol_version_supported_stateless(
        self, client: TestClient, stateless_version_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{stateless_version_server.id}/health-status")

        assert response.status_code == 200
        assert "MCP 2026-07-28 - supported" in response.text
        assert "bg-emerald-100" in response.text

    @pytest_asyncio.fixture
    async def newer_version_server(self, app):
        server = create_test_server(
            server_id="newer-version-server",
            healthy=True,
            last_checked=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            mcp_protocol_version="2099-01-01",
            mcp_conformant=False,
        )
        await app.state.registry.register(server)
        return server

    def test_mcp_protocol_version_newer_than_hub(
        self, client: TestClient, newer_version_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{newer_version_server.id}/health-status")

        assert response.status_code == 200
        assert "MCP 2099-01-01 - newer than hub" in response.text
        assert "bg-amber-100" in response.text

    def test_mcp_protocol_version_unsupported_shows_amber(
        self, client: TestClient, unhealthy_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{unhealthy_server.id}/health-status")

        assert response.status_code == 200
        assert "MCP 2024-11-05 - outdated" in response.text
        assert "bg-amber-100" in response.text

    def test_mcp_protocol_version_unsupported(
        self, client: TestClient, unhealthy_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{unhealthy_server.id}/health-status")

        assert response.status_code == 200
        assert "MCP 2024-11-05 - outdated" in response.text
        assert "bg-amber-100" in response.text

    def test_oauth_token_status_ok(
        self, client: TestClient, oauth_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{oauth_server.id}/health-status")

        assert response.status_code == 200
        assert "OAuth token: ok" in response.text
        assert "bg-emerald-100" in response.text

    def test_schema_valid_badge(self, client: TestClient, full_server: RegisteredServer) -> None:
        response = client.get(f"/api/servers/{full_server.id}/health-status")

        assert response.status_code == 200
        assert "Schema: valid" in response.text
        assert "bg-emerald-100" in response.text

    def test_schema_invalid_badge_with_issues(
        self, client: TestClient, schema_invalid_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{schema_invalid_server.id}/health-status")

        assert response.status_code == 200
        assert "Schema: issues" in response.text
        assert "bg-amber-100" in response.text
        assert "2" in response.text

    def test_uptime_badge_healthy_shows_time(
        self, client: TestClient, healthy_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{healthy_server.id}/health-status")

        assert response.status_code == 200
        assert "Uptime:" in response.text
        assert "bg-indigo-50" in response.text

    def test_uptime_badge_unhealthy_shows_dash(
        self, client: TestClient, unhealthy_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{unhealthy_server.id}/health-status")

        assert response.status_code == 200
        assert "Uptime: --" in response.text
        assert "bg-slate-100" in response.text

    def test_last_checked_timestamp_present(
        self, client: TestClient, healthy_server: RegisteredServer
    ) -> None:
        response = client.get(f"/api/servers/{healthy_server.id}/health-status")

        assert response.status_code == 200
        assert re.search(r"\d{2}:\d{2}:\d{2}", response.text) is not None
