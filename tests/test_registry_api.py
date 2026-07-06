import base64

import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import Settings, AuthConfig, BasicAuthConfig


def make_basic_auth_header(user: str, password: str) -> dict[str, str]:
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


class TestUnregisterServer:
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

    def test_without_auth_returns_401(self, client_with_basic_auth):
        response = client_with_basic_auth.delete("/v1/register/test-server")
        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == 'Basic realm="Restricted"'

    def test_delete_unknown_id_returns_500(self, client_with_basic_auth):
        response = client_with_basic_auth.delete(
            "/v1/register/nonexistent-id",
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 500
        assert response.text.startswith("unregister failed:")

    def test_delete_existing_id_returns_204(self, client_with_basic_auth):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "server-to-delete",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        response = client_with_basic_auth.delete(
            "/v1/register/server-to-delete",
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 204
        assert response.text == ""


class TestListServers:
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

    def test_get_v1_servers_returns_servers_wrapper(self, client_with_basic_auth):
        response = client_with_basic_auth.get("/v1/servers")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        data = response.json()
        assert "servers" in data
        assert isinstance(data["servers"], list)

    def test_get_api_servers_returns_servers_wrapper(self, client_with_basic_auth):
        response = client_with_basic_auth.get("/api/servers")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        data = response.json()
        assert "servers" in data
        assert isinstance(data["servers"], list)

    def test_v1_servers_and_api_servers_return_same_body(self, client_with_basic_auth):
        v1_response = client_with_basic_auth.get("/v1/servers")
        api_response = client_with_basic_auth.get("/api/servers")
        assert v1_response.json() == api_response.json()

    def test_after_register_one_server_returns_one_element(self, client_with_basic_auth):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "test-server",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        response = client_with_basic_auth.get("/v1/servers")
        assert response.status_code == 200
        data = response.json()
        assert len(data["servers"]) == 1
        assert data["servers"][0]["id"] == "test-server"

    def test_bearer_token_not_in_response(self, client_with_basic_auth):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "server-with-token",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
                "bearer_token": "secret-token-12345",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        v1_response = client_with_basic_auth.get("/v1/servers")
        api_response = client_with_basic_auth.get("/api/servers")

        assert "secret-token-12345" not in v1_response.text
        assert "secret-token-12345" not in api_response.text

    def test_oauth_client_secret_not_in_response(self, client_with_basic_auth):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "server-with-oauth",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
                "oauth_client_secret": "oauth-secret-12345",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        v1_response = client_with_basic_auth.get("/v1/servers")
        api_response = client_with_basic_auth.get("/api/servers")

        assert "oauth-secret-12345" not in v1_response.text
        assert "oauth-secret-12345" not in api_response.text

    def test_oauth_token_error_not_in_response(self, client_with_basic_auth):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "server-with-error",
                "url": "http://unreachable-host:9999",
                "registration_type": "self",
                "oauth_token_error": "token-error-12345",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        v1_response = client_with_basic_auth.get("/v1/servers")
        api_response = client_with_basic_auth.get("/api/servers")

        assert "token-error-12345" not in v1_response.text
        assert "token-error-12345" not in api_response.text


class TestGetServer:
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

    def test_get_unknown_id_returns_404(self, client_with_basic_auth):
        response = client_with_basic_auth.get("/v1/servers/nonexistent-id")
        assert response.status_code == 404
        assert response.text == "not found"

    def test_get_known_id_returns_200(self, client_with_basic_auth):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "test-server",
                "url": "http://example.com:8080",
                "registration_type": "self",
                "name": "Test Server",
                "version": "1.0.0",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        response = client_with_basic_auth.get("/v1/servers/test-server")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "test-server"
        assert data["url"] == "http://example.com:8080"
        assert data["name"] == "Test Server"
        assert data["version"] == "1.0.0"

    def test_get_server_matches_list_servers_shape(self, client_with_basic_auth):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "server-for-shape-check",
                "url": "http://example.com:8080",
                "registration_type": "self",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        get_response = client_with_basic_auth.get("/v1/servers/server-for-shape-check")
        list_response = client_with_basic_auth.get("/v1/servers")

        get_data = get_response.json()
        list_data = list_response.json()["servers"][0]

        assert get_data["id"] == list_data["id"]
        assert get_data["url"] == list_data["url"]
        assert get_data["name"] == list_data["name"]
        assert get_data["version"] == list_data["version"]

    def test_get_server_strips_bearer_token(self, client_with_basic_auth):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "server-with-token",
                "url": "http://example.com:8080",
                "registration_type": "self",
                "bearer_token": "secret-token-12345",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        response = client_with_basic_auth.get("/v1/servers/server-with-token")
        assert response.status_code == 200
        assert "secret-token-12345" not in response.text
        assert response.json()["bearer_token"] == ""

    def test_get_server_strips_oauth_client_secret(self, client_with_basic_auth):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "server-with-oauth-secret",
                "url": "http://example.com:8080",
                "registration_type": "self",
                "oauth_client_secret": "oauth-secret-12345",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        response = client_with_basic_auth.get("/v1/servers/server-with-oauth-secret")
        assert response.status_code == 200
        assert "oauth-secret-12345" not in response.text
        assert response.json()["oauth_client_secret"] == ""

    def test_get_server_strips_oauth_token_error(self, client_with_basic_auth):
        client_with_basic_auth.post(
            "/v1/register",
            json={
                "id": "server-with-token-error",
                "url": "http://example.com:8080",
                "registration_type": "self",
                "oauth_token_error": "token-error-12345",
            },
            headers=make_basic_auth_header("admin", "secret123"),
        )

        response = client_with_basic_auth.get("/v1/servers/server-with-token-error")
        assert response.status_code == 200
        assert "token-error-12345" not in response.text
        assert response.json()["oauth_token_error"] == ""
