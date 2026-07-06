import base64
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from mcp_hub.app import create_app
from mcp_hub.config import Settings, AuthConfig, BasicAuthConfig


def make_basic_auth_header(user: str, password: str) -> dict[str, str]:
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


class TestBasicAuthConfiguration:
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

    def test_protected_post_register_returns_401_without_credentials(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "test-server",
                "url": "http://unreachable-host-for-test:9999",
                "registration_type": "self",
            },
        )
        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers

    def test_protected_post_register_returns_401_with_invalid_credentials(
        self, client_with_basic_auth
    ):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "test-server",
                "url": "http://unreachable-host-for-test:9999",
                "registration_type": "self",
            },
            headers=make_basic_auth_header("wronguser", "wrongpass"),
        )
        assert response.status_code == 401

    def test_protected_post_register_succeeds_with_valid_credentials(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "test-server",
                "url": "http://unreachable-host-for-test:9999",
                "registration_type": "self",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 201

    def test_protected_delete_register_returns_401_without_credentials(
        self, client_with_basic_auth
    ):
        response = client_with_basic_auth.delete("/v1/register/test-server")
        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers

    def test_protected_delete_register_succeeds_with_valid_credentials(
        self, client_with_basic_auth
    ):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "test-server",
                "url": "http://unreachable-host-for-test:9999",
                "registration_type": "self",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )
        response = client_with_basic_auth.delete(
            "/v1/register/test-server",
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 204

    def test_protected_get_mcp_returns_401_without_credentials(self, client_with_basic_auth):
        response = client_with_basic_auth.get("/mcp")
        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers

    def test_protected_get_mcp_succeeds_with_valid_credentials(self, client_with_basic_auth):
        response = client_with_basic_auth.get(
            "/mcp", headers=make_basic_auth_header("admin", "secret123")
        )
        assert response.status_code == 200

    def test_protected_post_mcp_returns_401_without_credentials(self, client_with_basic_auth):
        response = client_with_basic_auth.post("/mcp", json={})
        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers

    def test_protected_post_mcp_succeeds_with_valid_credentials(self, client_with_basic_auth):
        mock_response = Response(200, content=b"{}", headers={"content-type": "application/json"})

        with patch("mcp_hub.routes.proxy.proxy_request", new_callable=AsyncMock) as mock_proxy:
            mock_proxy.return_value = mock_response

            response = client_with_basic_auth.post(
                "/mcp", json={}, headers=make_basic_auth_header("admin", "secret123")
            )
            assert response.status_code == 200

    def test_open_get_v1_servers_returns_200_without_credentials(self, client_with_basic_auth):
        response = client_with_basic_auth.get("/v1/servers")
        assert response.status_code == 200

    def test_open_get_api_servers_returns_200_without_credentials(self, client_with_basic_auth):
        response = client_with_basic_auth.get("/api/servers")
        assert response.status_code == 200

    def test_open_healthz_returns_200_without_credentials(self, client_with_basic_auth):
        response = client_with_basic_auth.get("/healthz")
        assert response.status_code == 200


class TestNoAuthConfiguration:
    @pytest.fixture
    def settings_with_no_auth(self) -> Settings:
        return Settings(
            auth=AuthConfig(
                type="none",
            )
        )

    @pytest.fixture
    def app_with_no_auth(self, settings_with_no_auth):
        return create_app(settings_with_no_auth)

    @pytest.fixture
    def client_with_no_auth(self, app_with_no_auth):
        return TestClient(app_with_no_auth, raise_server_exceptions=False)

    def test_protected_post_register_reachable_without_credentials(self, client_with_no_auth):
        response = client_with_no_auth.post(
            "/v1/register",
            json={
                "id": "test-server",
                "url": "http://unreachable-host-for-test:9999",
                "registration_type": "self",
            },
        )
        assert response.status_code == 201

    def test_protected_delete_register_reachable_without_credentials(self, client_with_no_auth):
        client_with_no_auth.post(
            "/v1/register",
            json={
                "id": "test-server",
                "url": "http://unreachable-host-for-test:9999",
                "registration_type": "self",
            },
        )
        response = client_with_no_auth.delete("/v1/register/test-server")
        assert response.status_code == 204

    def test_protected_get_mcp_reachable_without_credentials(self, client_with_no_auth):
        response = client_with_no_auth.get("/mcp")
        assert response.status_code == 200

    def test_protected_post_mcp_reachable_without_credentials(self, client_with_no_auth):
        mock_response = Response(200, content=b"{}", headers={"content-type": "application/json"})

        with patch("mcp_hub.routes.proxy.proxy_request", new_callable=AsyncMock) as mock_proxy:
            mock_proxy.return_value = mock_response

            response = client_with_no_auth.post("/mcp", json={})
            assert response.status_code == 200

    def test_open_get_v1_servers_returns_200_without_credentials(self, client_with_no_auth):
        response = client_with_no_auth.get("/v1/servers")
        assert response.status_code == 200

    def test_open_get_api_servers_returns_200_without_credentials(self, client_with_no_auth):
        response = client_with_no_auth.get("/api/servers")
        assert response.status_code == 200


class TestNoAuthAliasConfiguration:
    @pytest.fixture
    def settings_with_noauth_alias(self) -> Settings:
        return Settings(
            auth=AuthConfig(
                type="noauth",
            )
        )

    @pytest.fixture
    def app_with_noauth_alias(self, settings_with_noauth_alias):
        return create_app(settings_with_noauth_alias)

    @pytest.fixture
    def client_with_noauth_alias(self, app_with_noauth_alias):
        return TestClient(app_with_noauth_alias, raise_server_exceptions=False)

    def test_protected_endpoints_reachable_without_credentials(self, client_with_noauth_alias):
        response = client_with_noauth_alias.post(
            "/v1/register",
            json={
                "id": "test-server",
                "url": "http://unreachable-host-for-test:9999",
                "registration_type": "self",
            },
        )
        assert response.status_code == 201


class TestEmptyBasicCredentialsConfiguration:
    @pytest.fixture
    def settings_with_empty_basic_auth(self) -> Settings:
        return Settings(
            auth=AuthConfig(
                type="basic",
                basic_auth=BasicAuthConfig(register_user="", register_pass=""),
            )
        )

    @pytest.fixture
    def app_with_empty_basic_auth(self, settings_with_empty_basic_auth):
        return create_app(settings_with_empty_basic_auth)

    @pytest.fixture
    def client_with_empty_basic_auth(self, app_with_empty_basic_auth):
        return TestClient(app_with_empty_basic_auth, raise_server_exceptions=False)

    def test_all_endpoints_reachable_without_credentials(self, client_with_empty_basic_auth):
        mock_response = Response(200, content=b"{}", headers={"content-type": "application/json"})

        with patch("mcp_hub.routes.proxy.proxy_request", new_callable=AsyncMock) as mock_proxy:
            mock_proxy.return_value = mock_response

            response = client_with_empty_basic_auth.post(
                "/v1/register",
                json={
                    "id": "test-server",
                    "url": "http://unreachable-host-for-test:9999",
                    "registration_type": "self",
                },
            )
            assert response.status_code == 201

            response = client_with_empty_basic_auth.delete("/v1/register/test-server")
            assert response.status_code == 204

            response = client_with_empty_basic_auth.get("/mcp")
            assert response.status_code == 200

            response = client_with_empty_basic_auth.post("/mcp", json={})
            assert response.status_code == 200

            response = client_with_empty_basic_auth.get("/v1/servers")
            assert response.status_code == 200

            response = client_with_empty_basic_auth.get("/api/servers")
            assert response.status_code == 200
