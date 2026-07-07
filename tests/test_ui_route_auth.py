"""The powerful per-server UI routes (capabilities, tools, faults, initialize, playground,
trace) must honour basic auth when it is configured, and must stay open when auth.type is
'none' (the local-first default). Auth runs as a dependency before the handler, so an
unauthenticated request is rejected with 401 regardless of whether the server exists.
"""

import base64

import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import AuthConfig, BasicAuthConfig, Settings

# GET routes that should be protected; each is reachable without a registered server
# because the 401 is raised by the auth dependency before the handler runs.
PROTECTED_GET_ROUTES = [
    "/ui/server/s1/capabilities",
    "/ui/server/s1/tools",
    "/ui/server/s1/faults",
    "/ui/server/s1/initialize",
    "/ui/server/s1/playground",
    "/ui/server/s1/trace",
]


def basic_auth_header(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def basic_auth_client() -> TestClient:
    settings = Settings(
        auth=AuthConfig(
            type="basic",
            basic_auth=BasicAuthConfig(register_user="admin", register_pass="secret123"),
        )
    )
    return TestClient(create_app(settings), raise_server_exceptions=False)


@pytest.fixture
def noauth_client() -> TestClient:
    # Default local-first posture: auth.type == "none".
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.mark.parametrize("route", PROTECTED_GET_ROUTES)
def test_protected_ui_route_returns_401_without_credentials(
    basic_auth_client: TestClient, route: str
) -> None:
    response = basic_auth_client.get(route)
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


@pytest.mark.parametrize("route", PROTECTED_GET_ROUTES)
def test_protected_ui_route_not_401_with_valid_credentials(
    basic_auth_client: TestClient, route: str
) -> None:
    # With valid creds the auth dependency passes; the handler then 404s (no such server),
    # but crucially it is not a 401 — auth is satisfied.
    response = basic_auth_client.get(route, headers=basic_auth_header("admin", "secret123"))
    assert response.status_code != 401


@pytest.mark.parametrize("route", PROTECTED_GET_ROUTES)
def test_ui_route_open_when_auth_disabled(noauth_client: TestClient, route: str) -> None:
    # Local-first default must remain frictionless: no 401 when auth.type is "none".
    response = noauth_client.get(route)
    assert response.status_code != 401
