"""RFC 9728 protected-resource metadata (Story 5.4).

Without this an MCP client has no way to discover which authorization server guards
the hub; it would need the issuer configured out of band.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.auth.metadata import PROTECTED_RESOURCE_METADATA_PATH
from mcp_hub.config import AuthConfig, BasicAuthConfig, JWTAuthConfig, Settings

ISSUER = "https://idp.example.com/realms/mcp-hub"
JWKS_URI = "https://idp.example.com/realms/mcp-hub/protocol/openid-connect/certs"


def jwt_settings(**overrides: object) -> Settings:
    fields: dict[str, object] = {
        "issuer": ISSUER,
        "audience": "k5n-mcp-hub",
        "jwks_uri": JWKS_URI,
    }
    fields.update(overrides)
    return Settings(auth=AuthConfig(type="jwt", jwt=JWTAuthConfig(**fields)))  # type: ignore[arg-type]


def client_for(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), raise_server_exceptions=False)


class TestMetadataDocument:
    def test_served_when_auth_type_is_jwt(self) -> None:
        response = client_for(jwt_settings()).get(PROTECTED_RESOURCE_METADATA_PATH)

        assert response.status_code == 200
        assert response.json()["authorization_servers"] == [ISSUER]

    def test_requires_no_credentials(self) -> None:
        # Discovery metadata a client reads *before* it has a token.
        response = client_for(jwt_settings()).get(PROTECTED_RESOURCE_METADATA_PATH)

        assert response.status_code == 200

    def test_resource_defaults_to_the_request_base_url(self) -> None:
        response = client_for(jwt_settings()).get(PROTECTED_RESOURCE_METADATA_PATH)

        assert response.json()["resource"] == "http://testserver"

    def test_resource_honors_the_configured_override(self) -> None:
        # Behind a reverse proxy the request URL is not the public identity.
        settings = jwt_settings(resource="https://hub.example.com")

        response = client_for(settings).get(PROTECTED_RESOURCE_METADATA_PATH)

        assert response.json()["resource"] == "https://hub.example.com"

    def test_advertises_configured_scopes(self) -> None:
        settings = jwt_settings(required_scopes=["mcp:invoke", "mcp:read"])

        body = client_for(settings).get(PROTECTED_RESOURCE_METADATA_PATH).json()

        assert sorted(body["scopes_supported"]) == ["mcp:invoke", "mcp:read"]

    def test_bearer_methods_is_header_only(self) -> None:
        # Query-string and form-encoded token delivery are not accepted.
        body = client_for(jwt_settings()).get(PROTECTED_RESOURCE_METADATA_PATH).json()

        assert body["bearer_methods_supported"] == ["header"]


class TestNotServedWithoutJWT:
    @pytest.mark.parametrize(
        "auth",
        [
            AuthConfig(type="none"),
            AuthConfig(
                type="basic",
                basic_auth=BasicAuthConfig(register_user="admin", register_pass="secret"),
            ),
        ],
        ids=["none", "basic"],
    )
    def test_absent_unless_the_hub_is_a_resource_server(self, auth: AuthConfig) -> None:
        response = client_for(Settings(auth=auth)).get(PROTECTED_RESOURCE_METADATA_PATH)

        assert response.status_code == 404


class TestChallengePointsAtMetadata:
    def test_401_names_the_metadata_url(self) -> None:
        response = client_for(jwt_settings()).post(
            "/v1/register", json={"id": "s", "url": "http://example.invalid:9999"}
        )

        assert response.status_code == 401
        challenge = response.headers["WWW-Authenticate"]
        assert challenge.startswith("Bearer ")
        assert f'resource_metadata="http://testserver{PROTECTED_RESOURCE_METADATA_PATH}"' in (
            challenge
        )

    def test_basic_auth_challenge_is_unchanged(self) -> None:
        settings = Settings(
            auth=AuthConfig(
                type="basic",
                basic_auth=BasicAuthConfig(register_user="admin", register_pass="secret"),
            )
        )

        response = client_for(settings).post(
            "/v1/register", json={"id": "s", "url": "http://example.invalid:9999"}
        )

        assert response.headers["WWW-Authenticate"] == 'Basic realm="Restricted"'
