"""Registering a stdio server (Epic 9, Story 9.3).

A registration names an allowlist *entry*, never a command line. It also cannot ask
for on-behalf-of: one shared process cannot act as two callers, and pretending
otherwise would hand every caller the credentials the process started with.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import Settings


def _settings(**stdio: Any) -> Settings:
    base: dict[str, Any] = {
        "enabled": True,
        "allowed_commands": {"echo": {"command": "/usr/bin/true", "args": []}},
    }
    base.update(stdio)
    # jwt so the ADR 0007 startup gate is satisfied; the tests below drive
    # registration directly rather than through a token.
    return Settings(
        auth={
            "type": "jwt",
            "jwt": {"issuer": "https://i", "audience": "a", "jwks_uri": "https://j"},
        },
        stdio=base,
    )


def _register(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post("/v1/register", content=json.dumps(body))


@contextmanager
def as_admin() -> Any:
    """Build the app with every request authenticated as an admin.

    ADR 0007 forces stdio-enabled settings to `auth.type: jwt`, so these route tests
    need a principal holding the admin scope. It has to be patched at the build site:
    `auth_required` closes over the authenticator when the app is constructed, so
    assigning `app.state.authenticator` afterwards has no effect. Minting a real token
    and standing up a JWKS endpoint would test `jwt_bearer.py` over again, which
    tests/test_jwt_bearer.py already does.
    """
    from mcp_hub.auth.principal import Principal

    class _AdminAuthenticator:
        async def authenticate(self, request: Any) -> Principal:
            return Principal(
                subject="admin",
                issuer="https://i",
                scopes=frozenset({"mcp:admin"}),
                token="t",
            )

    with patch("mcp_hub.app.build_authenticator", return_value=_AdminAuthenticator()):
        yield


class TestStdioRegistration:
    def test_names_an_allowlist_entry_not_a_command(self) -> None:
        # The point of the allowlist: a caller supplies neither binary nor arguments.
        from mcp_hub.models.register_request import RegisterRequest

        assert "stdio_command_name" in RegisterRequest.model_fields
        for forbidden in ("command", "args", "env", "cwd"):
            assert forbidden not in RegisterRequest.model_fields, (
                f"{forbidden!r} must never be settable by a registration request"
            )

    def test_transport_kind_defaults_to_http(self) -> None:
        from mcp_hub.models.server import RegisteredServer

        srv = RegisteredServer(id="x", url="https://e.example.com/mcp")
        assert srv.transport_kind == "http"

    def test_stdio_server_gets_a_synthetic_url(self) -> None:
        """~43 call sites key off `server.url`. A synthetic `stdio:<name>` keeps them
        working and keeps the on-disk shape additive rather than migrated."""
        from mcp_hub.models.server import RegisteredServer

        srv = RegisteredServer(id="e", url="", transport_kind="stdio", stdio_command_name="echo")
        assert srv.url == "stdio:echo"
        assert srv.is_stdio is True


class TestStdioRefusesPerUserAuth:
    @pytest.mark.parametrize("auth_type", ["obo", "ema"])
    def test_obo_and_ema_are_rejected_for_stdio(self, auth_type: str) -> None:
        from mcp_hub.models.register_request import RegisterRequest

        with pytest.raises(ValueError) as exc:
            RegisterRequest.model_validate(
                {
                    "id": "e",
                    "url": "stdio:echo",
                    "transport_kind": "stdio",
                    "stdio_command_name": "echo",
                    "auth_type": auth_type,
                }
            )
        assert "stdio" in str(exc.value).lower()

    def test_service_credentials_are_still_allowed(self) -> None:
        from mcp_hub.models.register_request import RegisterRequest

        req = RegisterRequest.model_validate(
            {
                "id": "e",
                "url": "stdio:echo",
                "transport_kind": "stdio",
                "stdio_command_name": "echo",
                "auth_type": "bearer",
                "bearer_token": "t",
            }
        )
        assert req.auth_type == "bearer"


class TestStdioRegistrationRoute:
    def test_unknown_allowlist_entry_is_refused(self) -> None:
        with as_admin():
            app = create_app(_settings())
        with TestClient(app) as client:
            resp = _register(
                client,
                {
                    "id": "nope",
                    "transport_kind": "stdio",
                    "stdio_command_name": "not-in-the-allowlist",
                },
            )
        assert resp.status_code == 400
        assert "allow" in resp.text.lower()

    def test_stdio_registration_is_refused_when_the_feature_is_off(self) -> None:
        app = create_app(Settings(auth={"type": "none"}))  # stdio.enabled defaults False
        with TestClient(app) as client:
            resp = _register(
                client,
                {"id": "e", "transport_kind": "stdio", "stdio_command_name": "echo"},
            )
        assert resp.status_code == 400
        assert "stdio.enabled" in resp.text

    def test_allowlisted_entry_registers_without_url_validation(self) -> None:
        """A stdio server has no URL to resolve, so the SSRF reachability check that
        every HTTP registration goes through must not run against `stdio:echo`."""
        with as_admin():
            app = create_app(_settings())
        with TestClient(app) as client:
            resp = _register(
                client,
                {
                    "id": "e",
                    "transport_kind": "stdio",
                    "stdio_command_name": "echo",
                    "registration_type": "manual",
                },
            )
            assert resp.status_code == 201, resp.text
            listed = client.get("/v1/servers").json()

        servers = listed if isinstance(listed, list) else listed.get("servers", [])
        entry = next(s for s in servers if s["id"] == "e")
        assert entry["url"] == "stdio:echo"
        assert entry["transport_kind"] == "stdio"
