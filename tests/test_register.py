import base64
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from devhub.app import create_app
from devhub.config import Settings, AuthConfig, BasicAuthConfig


def make_basic_auth_header(user: str, password: str) -> dict[str, str]:
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


class TestRegisterServer:
    @pytest.fixture
    def settings_with_basic_auth(self) -> Settings:
        return Settings(
            auth=AuthConfig(
                type="basic",
                basic_auth=BasicAuthConfig(register_user="admin", register_pass="secret123"),
            )
        )

    @pytest.fixture
    def app_with_basic_auth(self, settings_with_basic_auth):
        return create_app(settings_with_basic_auth)

    @pytest.fixture
    def client_with_basic_auth(self, app_with_basic_auth):
        return TestClient(app_with_basic_auth, raise_server_exceptions=False)

    def test_no_auth_returns_401_with_www_authenticate_header(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={"id": "test-server", "url": "http://localhost:8080"},
        )
        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == 'Basic realm="Restricted"'

    def test_missing_id_returns_400_with_plain_text(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={"url": "http://localhost:8080"},
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 400
        assert response.text == "id and url required"

    def test_missing_url_returns_400_with_plain_text(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={"id": "test-server"},
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 400
        assert response.text == "id and url required"

    def test_empty_id_after_strip_returns_400(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={"id": "   ", "url": "http://localhost:8080"},
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 400
        assert response.text == "id and url required"

    def test_empty_url_after_strip_returns_400(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={"id": "test-server", "url": "   "},
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 400
        assert response.text == "id and url required"

    def test_invalid_json_returns_400_with_plain_text(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            content=b"not valid json",
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 400
        assert response.text == "invalid json"

    def test_self_registration_returns_201_immediately(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "self-server",
                "url": "http://unreachable-host-that-does-not-exist:9999",
                "registration_type": "self",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 201

    def test_manual_registration_with_unreachable_url_returns_400(self, client_with_basic_auth):
        with patch("devhub.mcp.discovery.DiscoveryService.discover_immediately") as mock_discover:
            mock_discover.side_effect = Exception("connection refused")

            response = client_with_basic_auth.post(
                "/v1/register",
                json={
                    "id": "manual-server",
                    "url": "http://localhost:9999",
                    "registration_type": "manual",
                },
                headers=make_basic_auth_header("admin", "secret123"),
            )

            assert response.status_code == 400
            assert "error" in response.json()

            get_response = client_with_basic_auth.get("/v1/servers")
            servers = get_response.json()
            assert not any(s["id"] == "manual-server" for s in servers["servers"])

    def test_response_body_has_bearer_token_redacted(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "bearer-server",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
                "bearer_token": "secret-token-123",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["bearer_token"] == ""

    def test_successful_registration_returns_201(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "new-server",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "new-server"
        assert data["url"] == "http://unreachable-host:9999"
        assert data["healthy"] is True
        assert data["consecutive_fails"] == 0

    def test_url_is_trimmed(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "trim-server",
                "url": "  http://unreachable-host:9999  ",
                "registration_type": "self",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["url"] == "http://unreachable-host:9999"

    def test_registration_type_defaulting_manual(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "type-server",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["registration_type"] == "self"

        with patch("devhub.mcp.discovery.DiscoveryService.discover_immediately") as mock_discover:
            mock_discover.side_effect = Exception("connection refused")

            response2 = client_with_basic_auth.post(
                "/v1/register",
                json={
                    "id": "type-server2",
                    "url": "http://unreachable-host:9999",
                },
                headers=make_basic_auth_header("admin", "secret123"),
            )

            assert response2.status_code == 400
            data2 = response2.json()
            assert "error" in data2

    def test_registration_type_preserved_from_existing(self, client_with_basic_auth):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "existing-type",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        response = client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "existing-type",
                "url": "http://unreachable-host2:9999",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["registration_type"] == "self"

    def test_auth_type_inferred_from_bearer_token(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "bearer-auth",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
                "bearer_token": "my-token",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["auth_type"] == "bearer"

    def test_auth_type_inferred_from_oauth_fields(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "oauth-auth",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
                "oauth_token_url": "http://localhost:8080/oauth/token",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["auth_type"] == "oauth"

    def test_oauth_fields_merged_from_existing(self, client_with_basic_auth):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "merge-server",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
                "oauth_client_id": "client-123",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        response = client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "merge-server",
                "url": "http://unreachable-host2:9999",
                "registration_type": "self",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["oauth_client_id"] == "client-123"

    def test_merge_preserves_existing_auth_fields(self, client_with_basic_auth):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "merge-server2",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
                "bearer_token": "existing-token",
                "oauth_client_id": "client-123",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        response = client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "merge-server2",
                "url": "http://unreachable-host2:9999",
                "registration_type": "self",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        assert response.status_code == 201
        data = response.json()
        assert data["bearer_token"] == ""
        assert data["oauth_client_id"] == "client-123"

    def test_oauth_discovery_failure_rolls_back_new_registration(self, client_with_basic_auth):
        with patch("devhub.mcp.oauth.discover_oauth_metadata") as mock_discover:
            mock_discover.side_effect = Exception("discovery failed")

            response = client_with_basic_auth.post(
                "/v1/register",
                json={
                    "id": "oauth-fail-new",
                    "url": "http://example.com:9999",
                    "registration_type": "manual",
                    "oauth_discovery_url": "http://example.com:9999/.well-known/oauth-authorization-server",
                },
                headers=make_basic_auth_header("admin", "secret123"),
            )

            assert response.status_code == 400
            assert response.json()["error"] == "oauth discovery failed"

            get_response = client_with_basic_auth.get("/v1/servers")
            servers = get_response.json()
            assert not any(s["id"] == "oauth-fail-new" for s in servers["servers"])

    def test_oauth_discovery_failure_rolls_back_existing_registration(self, client_with_basic_auth):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "oauth-fail-existing",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
                "oauth_client_id": "existing-client-id",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        with patch("devhub.mcp.oauth.discover_oauth_metadata") as mock_discover:
            mock_discover.side_effect = Exception("discovery failed")

            response = client_with_basic_auth.post(
                "/v1/register",
                json={
                    "id": "oauth-fail-existing",
                    "url": "http://example.com:9999",
                    "registration_type": "manual",
                    "oauth_discovery_url": "http://example.com:9999/.well-known/oauth-authorization-server",
                },
                headers=make_basic_auth_header("admin", "secret123"),
            )

            assert response.status_code == 400
            assert response.json()["error"] == "oauth discovery failed"

            get_response = client_with_basic_auth.get("/v1/servers")
            servers = get_response.json()
            assert any(s["id"] == "oauth-fail-existing" for s in servers["servers"])

    def test_oauth_discovery_failure_with_auth_type_oauth_rolls_back(self, client_with_basic_auth):
        with patch("devhub.mcp.oauth.discover_oauth_metadata") as mock_discover:
            mock_discover.side_effect = Exception("discovery failed")

            response = client_with_basic_auth.post(
                "/v1/register",
                json={
                    "id": "oauth-auth-type-fail",
                    "url": "http://example.com:9999",
                    "registration_type": "manual",
                    "auth_type": "oauth",
                    "oauth_discovery_url": "http://example.com:9999/.well-known/oauth-authorization-server",
                },
                headers=make_basic_auth_header("admin", "secret123"),
            )

            assert response.status_code == 400
            assert response.json()["error"] == "oauth discovery failed"

            get_response = client_with_basic_auth.get("/v1/servers")
            servers = get_response.json()
            assert not any(s["id"] == "oauth-auth-type-fail" for s in servers["servers"])
