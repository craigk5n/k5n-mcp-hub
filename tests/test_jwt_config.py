"""`auth.type: jwt` configuration and fail-closed construction (Story 5.3).

Also pins the ADR 0001 invariant: the hub runs with no identity provider, and an
unreachable one degrades requests rather than blocking startup.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.auth import build_authenticator
from mcp_hub.auth.jwt_bearer import DEFAULT_ALGORITHMS, JWTBearerStrategy
from mcp_hub.config import AuthConfig, JWTAuthConfig, Settings, load_settings

ISSUER = "https://idp.example.com/realms/mcp-hub"
JWKS_URI = "https://idp.example.com/realms/mcp-hub/protocol/openid-connect/certs"


def jwt_auth(**overrides: object) -> AuthConfig:
    fields: dict[str, object] = {
        "issuer": ISSUER,
        "audience": "k5n-mcp-hub",
        "jwks_uri": JWKS_URI,
    }
    fields.update(overrides)
    return AuthConfig(type="jwt", jwt=JWTAuthConfig(**fields))  # type: ignore[arg-type]


class TestJWTAuthConfig:
    def test_auth_type_jwt_is_accepted(self) -> None:
        assert AuthConfig(type="jwt").type == "jwt"

    def test_defaults_are_empty(self) -> None:
        config = JWTAuthConfig()

        assert config.issuer == ""
        assert config.audience == ""
        assert config.jwks_uri == ""
        assert config.algorithms == []
        assert config.required_scopes == []

    def test_default_auth_type_is_still_none(self) -> None:
        # The local-first default must not move (ADR 0001).
        assert Settings.from_defaults().auth.type == "none"


class TestBuildAuthenticator:
    def test_returns_a_jwt_strategy(self) -> None:
        assert isinstance(build_authenticator(jwt_auth()), JWTBearerStrategy)

    def test_empty_algorithms_falls_back_to_the_asymmetric_defaults(self) -> None:
        strategy = build_authenticator(jwt_auth())

        assert strategy._algorithms == DEFAULT_ALGORITHMS  # type: ignore[attr-defined]

    def test_missing_issuer_fails_closed(self) -> None:
        with pytest.raises(ValueError, match="auth.jwt.issuer"):
            build_authenticator(jwt_auth(issuer=""))

    def test_missing_audience_fails_closed(self) -> None:
        with pytest.raises(ValueError, match="auth.jwt.audience"):
            build_authenticator(jwt_auth(audience=""))

    def test_missing_jwks_uri_fails_closed(self) -> None:
        # Guessing the JWKS path from the issuer differs per IdP; requiring it is
        # honest and the error says so.
        with pytest.raises(ValueError, match="auth.jwt.jwks_uri"):
            build_authenticator(jwt_auth(jwks_uri=""))

    def test_symmetric_algorithm_is_refused(self) -> None:
        with pytest.raises(ValueError, match="asymmetric"):
            build_authenticator(jwt_auth(algorithms=["HS256"]))


class TestEnvOverrides:
    def test_nested_env_override(self, tmp_path) -> None:
        with patch.dict(os.environ, {"MCPHUB_AUTH__JWT__ISSUER": ISSUER}, clear=False):
            settings = load_settings(str(tmp_path / "missing.yaml"))

        assert settings.auth.jwt.issuer == ISSUER

    def test_env_override_does_not_blank_sibling_yaml_fields(self, tmp_path) -> None:
        # A shallow merge would replace the whole `jwt` subtree, silently discarding
        # the audience configured in YAML.
        config = tmp_path / "config.yaml"
        config.write_text(
            f"auth:\n  type: jwt\n  jwt:\n    audience: k5n-mcp-hub\n    jwks_uri: {JWKS_URI}\n"
        )

        with patch.dict(os.environ, {"MCPHUB_AUTH__JWT__ISSUER": ISSUER}, clear=False):
            settings = load_settings(str(config))

        assert settings.auth.jwt.issuer == ISSUER
        assert settings.auth.jwt.audience == "k5n-mcp-hub"
        assert settings.auth.jwt.jwks_uri == JWKS_URI

    def test_basic_auth_env_override_keeps_yaml_username(self, tmp_path) -> None:
        # Same fix, applied to the pre-existing basic_auth subtree.
        config = tmp_path / "config.yaml"
        config.write_text("auth:\n  type: basic\n  basic_auth:\n    register_user: operator\n")

        with patch.dict(
            os.environ, {"MCPHUB_AUTH__BASIC_AUTH__REGISTER_PASS": "s3cret"}, clear=False
        ):
            settings = load_settings(str(config))

        assert settings.auth.basic_auth.register_user == "operator"
        assert settings.auth.basic_auth.register_pass == "s3cret"


class TestRunsWithoutAnIdentityProvider:
    """ADR 0001's hard invariant."""

    def test_default_settings_start_and_serve_with_no_idp(self) -> None:
        app = create_app(Settings.from_defaults())
        client = TestClient(app, raise_server_exceptions=False)

        assert client.get("/healthz").status_code == 200
        assert client.get("/api/servers").status_code == 200
        # Unchanged from today: no credentials needed, no IdP consulted.
        assert (
            client.post(
                "/v1/register",
                json={
                    "id": "test-server",
                    "url": "http://unreachable-host-for-test:9999",
                    "registration_type": "self",
                },
            ).status_code
            == 201
        )

    def test_jwt_configured_with_an_unreachable_idp_still_starts(self) -> None:
        # JWKS is lazy, so a down IdP must not stop the app being created.
        app = create_app(Settings(auth=jwt_auth()))
        client = TestClient(app, raise_server_exceptions=False)

        assert client.get("/healthz").status_code == 200

    def test_unreachable_idp_yields_401_not_a_crash(self) -> None:
        app = create_app(Settings(auth=jwt_auth()))
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/v1/register",
            json={"id": "s", "url": "http://unreachable-host-for-test:9999"},
            headers={"Authorization": "Bearer some.opaque.token"},
        )

        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers
