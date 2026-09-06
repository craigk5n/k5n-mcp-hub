"""Multi-tenant authorization through the real app with real signed tokens.

Every finding this session that mattered was invisible to unit tests and visible
end to end, so these drive the actual HTTP surface: two users, real JWTs, real
routes.
"""

from __future__ import annotations

import json
import time
from typing import Any

import anyio
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.auth.authorize import DEFAULT_ADMIN_SCOPE
from mcp_hub.config import AuthConfig, HealthCheckConfig, JWTAuthConfig, Settings
from mcp_hub.models.server import RegisteredServer

ISSUER = "https://idp.example.com"
AUDIENCE = "k5n-mcp-hub"
JWKS_URI = "https://idp.example.com/certs"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token_for(subject: str, *scopes: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": subject,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + 300,
            "scope": " ".join(scopes),
        },
        KEY,
        algorithm="RS256",
        headers={"kid": "k1"},
    )


def jwks_client() -> httpx.AsyncClient:
    entry = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    entry.update({"kid": "k1", "use": "sig", "alg": "RS256"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [entry]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def client():
    settings = Settings(
        auth=AuthConfig(
            type="jwt",
            jwt=JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, jwks_uri=JWKS_URI),
        ),
        # These servers point at unroutable hosts; a background probe left in flight
        # at shutdown makes the teardown flaky and tests nothing here.
        healthcheck=HealthCheckConfig(interval_seconds=3600),
    )
    app = create_app(settings)
    # Serve JWKS from a mock so no network is needed.
    app.state.authenticator._client = jwks_client()  # type: ignore[attr-defined]

    for server in (
        RegisteredServer(id="files", url="http://files.invalid/mcp", required_scope="files:use"),
        RegisteredServer(
            id="secrets", url="http://secrets.invalid/mcp", required_scope="secrets:use"
        ),
        RegisteredServer(id="unlabelled", url="http://unlabelled.invalid/mcp"),
    ):
        anyio.run(app.state.registry.register, server)

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def call(client: TestClient, token: str, server_id: str) -> Any:
    return client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "X-MCP-Target-Server": server_id,
            "Content-Type": "application/json",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )


class TestPerServerAccess:
    def test_a_user_may_reach_the_server_their_scope_names(self, client) -> None:
        # 502 means it got past authorization and tried the (unreachable) backend.
        response = call(client, token_for("alice", "files:use"), "files")

        assert response.status_code != 403

    def test_a_user_may_not_reach_another_teams_server(self, client) -> None:
        # The finding this whole change closes: alice authenticates fine, and
        # previously that was enough to use the hub's credential for any server.
        response = call(client, token_for("alice", "files:use"), "secrets")

        assert response.status_code == 403
        assert "secrets:use" in response.text

    def test_an_unlabelled_server_is_reachable_by_nobody(self, client) -> None:
        response = call(client, token_for("alice", "files:use"), "unlabelled")

        assert response.status_code == 403

    def test_an_admin_reaches_everything(self, client) -> None:
        admin = token_for("root", DEFAULT_ADMIN_SCOPE)

        for server_id in ("files", "secrets", "unlabelled"):
            assert call(client, admin, server_id).status_code != 403

    def test_an_unauthenticated_caller_gets_401_not_403(self, client) -> None:
        # 403 would tell an anonymous caller they lack a scope; the useful answer is
        # "authenticate, here is where".
        response = client.post(
            "/mcp",
            headers={"X-MCP-Target-Server": "files", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

        assert response.status_code == 401


class TestAdminOperations:
    def test_an_ordinary_user_cannot_register_a_server(self, client) -> None:
        response = client.post(
            "/v1/register",
            headers={"Authorization": f"Bearer {token_for('alice', 'files:use')}"},
            json={"id": "new", "url": "http://new.invalid/mcp", "registration_type": "self"},
        )

        assert response.status_code == 403

    def test_an_ordinary_user_cannot_delete_a_server(self, client) -> None:
        response = client.delete(
            "/v1/register/files",
            headers={"Authorization": f"Bearer {token_for('alice', 'files:use')}"},
        )

        assert response.status_code == 403

    def test_an_ordinary_user_cannot_inject_faults(self, client) -> None:
        # Fault injection is a denial-of-service primitive against every other caller
        # of that server, so it is an admin operation regardless of who may call it.
        response = client.post(
            "/ui/server/files/faults",
            headers={"Authorization": f"Bearer {token_for('alice', 'files:use')}"},
            data={"enabled": "on"},
        )

        assert response.status_code == 403

    def test_an_admin_can_register(self, client) -> None:
        response = client.post(
            "/v1/register",
            headers={"Authorization": f"Bearer {token_for('root', DEFAULT_ADMIN_SCOPE)}"},
            json={"id": "new", "url": "http://new.invalid/mcp", "registration_type": "self"},
        )

        assert response.status_code in (200, 201)


class TestDenialsAreAudited:
    def test_a_refused_call_is_recorded_with_the_subject(self, client) -> None:
        call(client, token_for("mallory", "files:use"), "secrets")

        entries = client.app.state.trace_recorder.list("secrets")
        assert entries, "a denied attempt must still be recorded"
        assert entries[0].status == 403
        assert entries[0].subject == "mallory"
