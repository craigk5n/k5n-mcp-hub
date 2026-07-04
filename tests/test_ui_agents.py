import base64
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from devhub.app import create_app
from devhub.config import Settings, AuthConfig, BasicAuthConfig


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
def client() -> TestClient:
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


def test_fixture_crud_cycle(client: TestClient) -> None:
    agent_id = "test-agent"
    fixture_name = "test-fixture"
    fixture_body = '{"key": "value"}'

    response = client.post(
        f"/ui/agent/{agent_id}/fixtures",
        data={"name": fixture_name, "body": fixture_body},
    )
    assert response.status_code == 204

    response = client.get(f"/ui/agent/{agent_id}/fixtures")
    assert response.status_code == 200
    data = response.json()
    assert fixture_name in data["fixtures"]

    response = client.get(f"/ui/agent/{agent_id}/fixtures/{fixture_name}")
    assert response.status_code == 200
    data = response.json()
    assert data["body"] == fixture_body

    response = client.delete(f"/ui/agent/{agent_id}/fixtures/{fixture_name}")
    assert response.status_code == 204

    response = client.get(f"/ui/agent/{agent_id}/fixtures")
    assert response.status_code == 200
    data = response.json()
    assert fixture_name not in data["fixtures"]


def test_get_missing_fixture_returns_404(client: TestClient) -> None:
    response = client.get("/ui/agent/nonexistent-agent/fixtures/missing-fixture")
    assert response.status_code == 404


def test_delete_missing_fixture_returns_404(client: TestClient) -> None:
    response = client.delete("/ui/agent/nonexistent-agent/fixtures/missing-fixture")
    assert response.status_code == 404


def test_list_fixtures_sorted(client: TestClient) -> None:
    agent_id = "test-agent-sort"
    client.post(f"/ui/agent/{agent_id}/fixtures", data={"name": "charlie", "body": "c"})
    client.post(f"/ui/agent/{agent_id}/fixtures", data={"name": "alpha", "body": "a"})
    client.post(f"/ui/agent/{agent_id}/fixtures", data={"name": "bravo", "body": "b"})

    response = client.get(f"/ui/agent/{agent_id}/fixtures")
    assert response.status_code == 200
    data = response.json()
    assert data["fixtures"] == ["alpha", "bravo", "charlie"]


def test_fixture_overwrite(client: TestClient) -> None:
    agent_id = "test-agent-overwrite"
    fixture_name = "overwrite-fixture"

    client.post(f"/ui/agent/{agent_id}/fixtures", data={"name": fixture_name, "body": "v1"})
    response = client.get(f"/ui/agent/{agent_id}/fixtures/{fixture_name}")
    assert response.json()["body"] == "v1"

    client.post(f"/ui/agent/{agent_id}/fixtures", data={"name": fixture_name, "body": "v2"})
    response = client.get(f"/ui/agent/{agent_id}/fixtures/{fixture_name}")
    assert response.json()["body"] == "v2"


class TestRegisterAgent:
    def test_no_auth_returns_401(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/agents/register",
            json={"id": "test-agent", "url": "http://localhost:8080"},
        )
        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == 'Basic realm="Restricted"'

    def test_missing_id_returns_400(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/agents/register",
            json={"url": "http://localhost:8080"},
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 400
        assert response.json()["error"] == "id and url required"

    def test_missing_url_returns_400(self, client_with_basic_auth):
        response = client_with_basic_auth.post(
            "/v1/agents/register",
            json={"id": "test-agent"},
            headers=make_basic_auth_header("admin", "secret123"),
        )
        assert response.status_code == 400
        assert response.json()["error"] == "id and url required"

    def test_valid_registration_returns_201(self, client_with_basic_auth):
        with patch("devhub.routes.ui_agents.refresh_agent_card") as mock_refresh:
            mock_refresh.return_value = None
            response = client_with_basic_auth.post(
                "/v1/agents/register",
                json={
                    "id": "my-agent",
                    "url": "http://localhost:8080",
                    "name": "My Agent",
                    "description": "A test agent",
                    "tags": ["test", "demo"],
                },
                headers=make_basic_auth_header("admin", "secret123"),
            )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "my-agent"
        assert data["url"] == "http://localhost:8080"
        assert data["name"] == "My Agent"
        assert data["description"] == "A test agent"
        assert data["tags"] == ["test", "demo"]

    def test_response_body_has_bearer_token_redacted(self, client_with_basic_auth):
        with patch("devhub.routes.ui_agents.refresh_agent_card") as mock_refresh:
            mock_refresh.return_value = None
            response = client_with_basic_auth.post(
                "/v1/agents/register",
                json={
                    "id": "agent-with-token",
                    "url": "http://localhost:8080",
                    "bearer_token": "secret-token-123",
                },
                headers=make_basic_auth_header("admin", "secret123"),
            )

        assert response.status_code == 201
        data = response.json()
        assert data["bearer_token"] == ""

    def test_card_refresh_failure_does_not_fail_registration(self, client_with_basic_auth):
        with patch("devhub.routes.ui_agents.refresh_agent_card") as mock_refresh:
            mock_refresh.side_effect = Exception("Network error")
            response = client_with_basic_auth.post(
                "/v1/agents/register",
                json={
                    "id": "agent-card-fail",
                    "url": "http://localhost:8080",
                },
                headers=make_basic_auth_header("admin", "secret123"),
            )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "agent-card-fail"
