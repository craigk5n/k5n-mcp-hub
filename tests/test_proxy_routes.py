import base64
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from devhub.app import create_app
from devhub.config import Settings, AuthConfig, BasicAuthConfig
from devhub.proxy.handler import proxy_request
from devhub.routes import proxy as proxy_module


def make_basic_auth_header(user: str, password: str) -> dict[str, str]:
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


@pytest.fixture
def settings_with_basic_auth() -> Settings:
    return Settings(
        auth=AuthConfig(
            type="basic",
            basic_auth=BasicAuthConfig(register_user="admin", register_pass="secret123"),
        )
    )


@pytest.fixture
def app_with_basic_auth(settings_with_basic_auth):
    return create_app(settings_with_basic_auth)


@pytest.fixture
def client_with_basic_auth(app_with_basic_auth):
    return TestClient(app_with_basic_auth, raise_server_exceptions=False)


@pytest.fixture
def settings_with_no_auth() -> Settings:
    return Settings(
        auth=AuthConfig(
            type="none",
        )
    )


@pytest.fixture
def app_with_no_auth(settings_with_no_auth):
    return create_app(settings_with_no_auth)


@pytest.fixture
def client_with_no_auth(app_with_no_auth):
    return TestClient(app_with_no_auth, raise_server_exceptions=False)


class TestProxyRoutesAuthRequired:
    def test_proxy_post_mcp_returns_401_without_credentials(self, client_with_basic_auth):
        response = client_with_basic_auth.post("/mcp", json={})
        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers

    def test_proxy_post_mcp_with_session_returns_401_without_credentials(
        self, client_with_basic_auth
    ):
        response = client_with_basic_auth.post("/mcp/test-session", json={})
        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers

    def test_proxy_get_mcp_with_session_returns_401_without_credentials(
        self, client_with_basic_auth
    ):
        response = client_with_basic_auth.get("/mcp/test-session")
        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers


class TestProxyRoutesDispatch:
    def test_proxy_post_mcp_dispatches_to_proxy_request(self, app_with_no_auth):
        mock_response = Response(200, content=b"ok", headers={"content-type": "text/plain"})
        mock_proxy = AsyncMock(return_value=mock_response)

        app_with_no_auth.dependency_overrides[proxy_module.get_proxy_handler] = lambda: mock_proxy

        client = TestClient(app_with_no_auth, raise_server_exceptions=False)
        _ = client.post("/mcp", json={})

        assert mock_proxy.call_count == 1

        app_with_no_auth.dependency_overrides.clear()

    def test_proxy_post_mcp_with_session_dispatches_to_proxy_request(self, app_with_no_auth):
        mock_response = Response(200, content=b"ok", headers={"content-type": "text/plain"})
        mock_proxy = AsyncMock(return_value=mock_response)

        app_with_no_auth.dependency_overrides[proxy_module.get_proxy_handler] = lambda: mock_proxy

        client = TestClient(app_with_no_auth, raise_server_exceptions=False)
        _ = client.post("/mcp/test-session-id", json={})

        assert mock_proxy.call_count == 1

        app_with_no_auth.dependency_overrides.clear()

    def test_proxy_get_mcp_with_session_dispatches_to_proxy_request(self, app_with_no_auth):
        mock_response = Response(200, content=b"ok", headers={"content-type": "text/plain"})
        mock_proxy = AsyncMock(return_value=mock_response)

        app_with_no_auth.dependency_overrides[proxy_module.get_proxy_handler] = lambda: mock_proxy

        client = TestClient(app_with_no_auth, raise_server_exceptions=False)
        _ = client.get("/mcp/test-session-id")

        assert mock_proxy.call_count == 1

        app_with_no_auth.dependency_overrides.clear()


class TestProxyRoutesNoAuth:
    def test_proxy_post_mcp_reachable_without_credentials(self, client_with_no_auth):
        mock_response = Response(200, content=b"ok", headers={"content-type": "text/plain"})

        with patch("devhub.routes.proxy.proxy_request", new_callable=AsyncMock) as mock_proxy:
            mock_proxy.return_value = mock_response

            response = client_with_no_auth.post("/mcp", json={})

            assert response.status_code == 200

    def test_proxy_post_mcp_with_session_reachable_without_credentials(self, client_with_no_auth):
        mock_response = Response(200, content=b"ok", headers={"content-type": "text/plain"})

        with patch("devhub.routes.proxy.proxy_request", new_callable=AsyncMock) as mock_proxy:
            mock_proxy.return_value = mock_response

            response = client_with_no_auth.post("/mcp/test-session", json={})

            assert response.status_code == 200

    def test_proxy_get_mcp_with_session_reachable_without_credentials(self, client_with_no_auth):
        mock_response = Response(200, content=b"ok", headers={"content-type": "text/plain"})

        with patch("devhub.routes.proxy.proxy_request", new_callable=AsyncMock) as mock_proxy:
            mock_proxy.return_value = mock_response

            response = client_with_no_auth.get("/mcp/test-session")

            assert response.status_code == 200
