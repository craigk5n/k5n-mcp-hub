"""Per-server and admin authorization (multi-tenant mode).

The hub authenticates callers under `auth.type: jwt` but, until now, did nothing
with the distinction: any authenticated caller could reach any registered server and
perform any admin operation. These are the rules that close that.
"""

from __future__ import annotations

import pytest

from mcp_hub.auth.authorize import (
    DEFAULT_ADMIN_SCOPE,
    authorization_enforced,
    is_admin,
    may_call_server,
)
from mcp_hub.auth.principal import Principal
from mcp_hub.config import AuthConfig, BasicAuthConfig, JWTAuthConfig, Settings
from mcp_hub.models.server import RegisteredServer


def jwt_settings(**jwt_overrides) -> Settings:
    fields = {
        "issuer": "https://idp.example.com",
        "audience": "k5n-mcp-hub",
        "jwks_uri": "https://idp.example.com/certs",
    }
    fields.update(jwt_overrides)
    return Settings(auth=AuthConfig(type="jwt", jwt=JWTAuthConfig(**fields)))


def user(*scopes: str) -> Principal:
    return Principal(subject="alice", issuer="https://idp.example.com", scopes=frozenset(scopes))


def server(required_scope: str = "") -> RegisteredServer:
    return RegisteredServer(id="files", url="https://x/mcp", required_scope=required_scope)


class TestEnforcementIsScopedToJwtMode:
    """`none` and `basic` are single-user modes; there is no tenancy to enforce."""

    def test_not_enforced_under_auth_none(self) -> None:
        assert authorization_enforced(Settings.from_defaults().auth) is False

    def test_not_enforced_under_basic(self) -> None:
        auth = AuthConfig(
            type="basic", basic_auth=BasicAuthConfig(register_user="a", register_pass="b")
        )

        assert authorization_enforced(auth) is False

    def test_enforced_under_jwt(self) -> None:
        assert authorization_enforced(jwt_settings().auth) is True

    def test_everything_is_allowed_when_not_enforced(self) -> None:
        auth = Settings.from_defaults().auth

        assert may_call_server(Principal.anonymous(), server(), auth) is True
        assert is_admin(Principal.anonymous(), auth) is True


class TestPerServerAccess:
    def test_a_server_with_no_rule_is_closed_under_jwt(self) -> None:
        # The chosen default: safe rather than additive. An unlabelled server is
        # reachable by nobody until an operator says who may reach it.
        assert may_call_server(user("mcp:read"), server(), jwt_settings().auth) is False

    def test_a_caller_with_the_required_scope_is_allowed(self) -> None:
        assert may_call_server(user("files:use"), server("files:use"), jwt_settings().auth) is True

    def test_a_caller_without_it_is_denied(self) -> None:
        assert may_call_server(user("other:use"), server("files:use"), jwt_settings().auth) is False

    def test_an_admin_may_reach_any_server(self) -> None:
        # Honest rather than notional: an admin can edit the server's required_scope
        # or its credentials, so pretending they cannot reach it buys nothing.
        admin = user(DEFAULT_ADMIN_SCOPE)

        assert may_call_server(admin, server("files:use"), jwt_settings().auth) is True
        assert may_call_server(admin, server(), jwt_settings().auth) is True

    def test_an_anonymous_principal_is_denied(self) -> None:
        assert (
            may_call_server(Principal.anonymous(), server("files:use"), jwt_settings().auth)
            is False
        )


class TestAdminScope:
    def test_the_configured_scope_grants_admin(self) -> None:
        auth = jwt_settings(admin_scope="ops:hub").auth

        assert is_admin(user("ops:hub"), auth) is True
        assert is_admin(user("mcp:admin"), auth) is False, "the default must not also apply"

    def test_the_default_scope_applies_when_unset(self) -> None:
        auth = jwt_settings().auth

        assert is_admin(user(DEFAULT_ADMIN_SCOPE), auth) is True
        assert is_admin(user("something:else"), auth) is False

    def test_an_ordinary_caller_is_not_admin(self) -> None:
        assert is_admin(user("files:use"), jwt_settings().auth) is False

    def test_anonymous_is_not_admin_under_jwt(self) -> None:
        assert is_admin(Principal.anonymous(), jwt_settings().auth) is False


class TestModel:
    def test_required_scope_defaults_to_empty(self) -> None:
        assert RegisteredServer(id="s", url="http://x").required_scope == ""

    def test_register_request_accepts_it(self) -> None:
        from mcp_hub.models.register_request import RegisterRequest

        assert (
            RegisterRequest(id="s", url="http://x", required_scope="files:use").required_scope
            == "files:use"
        )

    def test_it_is_not_a_secret_and_survives_sanitizing(self) -> None:
        # An operator needs to see which scope a server requires in order to grant it.
        s = RegisteredServer(id="s", url="http://x", required_scope="files:use")

        assert s.sanitize_for_api().required_scope == "files:use"
        assert s.sanitize_for_persistence().required_scope == "files:use"
