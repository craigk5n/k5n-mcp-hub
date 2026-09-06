"""Regressions from the adversarial pass over the auth surface.

Each test here corresponds to something that was actually reachable, not a
hypothetical: the probes are recorded in the commit message.
"""

from __future__ import annotations

import anyio
import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import Settings
from mcp_hub.mcp.obo_cache import OBOCacheKey
from mcp_hub.models.server import RegisteredServer

LEAKED = "SECRET-IDP-ECHO-jkl012"


def server_with_error(**overrides) -> RegisteredServer:
    fields = {
        "id": "victim",
        "url": "https://backend.example/mcp",
        "name": "Victim",
        "auth_type": "obo",
        "oauth_client_id": "hub-client",
        "oauth_client_secret": "hub-secret",
        "obo_audience": "backend",
        "obo_status": "error",
        "obo_error": f"invalid_grant: subject_token '{LEAKED}' is not active",
    }
    fields.update(overrides)
    return RegisteredServer(**fields)


class TestIdPErrorTextIsRedactedInTheUI:
    """F2: sanitize_for_api scrubs obo_error precisely because IdP error text can
    echo token material — and the servers page rendered it raw."""

    def _servers_page(self, server: RegisteredServer) -> str:
        app = create_app(Settings.from_defaults())
        anyio.run(app.state.registry.register, server)
        with TestClient(app, raise_server_exceptions=False) as client:
            return client.get("/ui/servers").text

    def test_token_material_in_obo_error_does_not_reach_the_page(self) -> None:
        assert LEAKED not in self._servers_page(server_with_error())

    def test_token_material_in_ema_error_does_not_reach_the_page(self) -> None:
        server = server_with_error(
            auth_type="ema",
            ema_resource_as_issuer="https://backend.example/oauth",
            ema_status="error",
            ema_error=f"leg 1: invalid_grant: assertion '{LEAKED}' rejected",
        )

        assert LEAKED not in self._servers_page(server)

    def test_the_diagnostic_itself_survives(self) -> None:
        # Redaction must not cost the operator the reason: a scrubbed page that says
        # nothing is as unhelpful as no page.
        page = self._servers_page(server_with_error())

        assert "invalid_grant" in page


class TestCacheKeyCoversEveryTokenShapingField:
    """F4: a field that changes the issued token but is absent from the key means a
    config change silently keeps serving tokens minted under the old settings."""

    @pytest.mark.parametrize(
        "field", ["subject", "issuer", "server_id", "audience", "scope", "resource", "flow"]
    )
    def test_key_field_is_present(self, field: str) -> None:
        assert field in OBOCacheKey.__dataclass_fields__

    @pytest.mark.asyncio
    async def test_changing_the_resource_indicator_does_not_reuse_a_token(self) -> None:
        from mcp_hub.mcp.auth import obo_cache_key
        from mcp_hub.auth.principal import Principal

        caller = Principal(subject="alice", issuer="https://idp", token="t")
        before = obo_cache_key(server_with_error(obo_resource="https://a/mcp"), caller)
        after = obo_cache_key(server_with_error(obo_resource="https://b/mcp"), caller)

        assert before != after, "a changed resource indicator must miss the cache"

    @pytest.mark.asyncio
    async def test_changing_the_ema_subject_token_type_does_not_reuse_a_token(self) -> None:
        # id_token and access_token produce different IdP policy evaluation, so a
        # cached token from one must never satisfy the other.
        from mcp_hub.mcp.auth import obo_cache_key
        from mcp_hub.auth.principal import Principal

        caller = Principal(subject="alice", issuer="https://idp", token="t", id_token="i")
        base = {
            "auth_type": "ema",
            "ema_resource_as_issuer": "https://backend.example/oauth",
            "ema_resource_as_token_url": "https://backend.example/oauth/token",
        }
        before = obo_cache_key(server_with_error(**base, ema_subject_token_type="id_token"), caller)
        after = obo_cache_key(
            server_with_error(**base, ema_subject_token_type="access_token"), caller
        )

        assert before != after
