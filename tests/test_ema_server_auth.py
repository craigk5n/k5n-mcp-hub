"""`auth_type: "ema"` through apply_server_auth (Story 8.4).

Mirrors the OBO rule: exact auth_type match, fails closed, background callers fall
through to the static credentials.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from mcp_hub.auth.caller import SERVICE_IDENTITY
from mcp_hub.auth.principal import Principal
from mcp_hub.mcp.auth import OBOAuthError, apply_server_auth, needs_user_identity
from mcp_hub.mcp.id_jag import ID_JAG_TOKEN_TYPE, IDJagAccessToken, IDJagError
from mcp_hub.mcp.obo_cache import OBOTokenCache
from mcp_hub.models.register_request import RegisterRequest
from mcp_hub.models.server import RegisteredServer

IDP_TOKEN_URL = "https://idp.example.com/token"
RESOURCE_AS_ISSUER = "https://backend.example.com/oauth"
RESOURCE_AS_TOKEN_URL = "https://backend.example.com/oauth/token"
RESOURCE_ID = "https://files.example.com/mcp"


def ema_server(**overrides: Any) -> RegisteredServer:
    fields: dict[str, Any] = {
        "id": "files",
        "url": RESOURCE_ID,
        "auth_type": "ema",
        "oauth_token_url": IDP_TOKEN_URL,
        "oauth_client_id": "k5n-mcp-hub",
        "oauth_client_secret": "hub-secret",
        "ema_resource_as_issuer": RESOURCE_AS_ISSUER,
        "ema_resource_as_token_url": RESOURCE_AS_TOKEN_URL,
        "ema_resource_id": RESOURCE_ID,
    }
    fields.update(overrides)
    return RegisteredServer(**fields)


def alice(*, id_token: str = "alice-id-token") -> Principal:
    return Principal(
        subject="alice",
        issuer="https://idp.example.com",
        token="alice-access-token",
        id_token=id_token,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def stub_id_jag(access_token: str = "downstream-token"):
    seen: list[Any] = []

    async def run(request, **kwargs):
        seen.append(request)
        return IDJagAccessToken(access_token=access_token, expires_in=300)

    run.seen = seen  # type: ignore[attr-defined]
    return run


class TestModel:
    def test_server_accepts_ema_configuration(self) -> None:
        server = ema_server()

        assert server.auth_type == "ema"
        assert server.ema_resource_as_issuer == RESOURCE_AS_ISSUER
        assert server.ema_subject_token_type == "id_token"

    def test_register_request_accepts_ema(self) -> None:
        request = RegisterRequest(
            id="files",
            url=RESOURCE_ID,
            auth_type="ema",
            ema_resource_as_issuer=RESOURCE_AS_ISSUER,
            ema_resource_as_token_url=RESOURCE_AS_TOKEN_URL,
        )

        assert request.auth_type == "ema"

    def test_sanitize_for_api_scrubs_the_error_detail(self) -> None:
        server = ema_server(ema_status="error", ema_error="leg 1: access_denied")

        assert server.sanitize_for_api().ema_error == ""

    def test_sanitize_for_persistence_clears_runtime_state(self) -> None:
        persisted = ema_server(ema_status="ok", ema_error="stale").sanitize_for_persistence()

        assert persisted.ema_status == ""
        assert persisted.ema_error == ""

    def test_no_assertion_is_ever_persisted(self) -> None:
        blob = json.dumps(ema_server().sanitize_for_persistence().model_dump(mode="json"))

        assert "alice-id-token" not in blob
        assert "downstream-token" not in blob

    def test_grant_profile_support_is_recorded(self) -> None:
        # Story 8.1's advisory field: unknown by default, never a gate.
        assert ema_server().ema_supports_id_jag_profile is None


class TestUserPath:
    @pytest.mark.asyncio
    async def test_runs_the_two_leg_flow_and_sets_authorization(self) -> None:
        headers: dict[str, str] = {}
        run = stub_id_jag()

        await apply_server_auth(
            headers, ema_server(), caller=alice(), obo_cache=OBOTokenCache(), id_jag=run
        )

        assert headers["Authorization"] == "Bearer downstream-token"
        assert "X-MCP-Token" not in headers

    @pytest.mark.asyncio
    async def test_passes_the_resource_as_issuer_as_the_leg_one_audience(self) -> None:
        run = stub_id_jag()

        await apply_server_auth(
            {}, ema_server(), caller=alice(), obo_cache=OBOTokenCache(), id_jag=run
        )

        sent = run.seen[0]  # type: ignore[attr-defined]
        assert sent.resource_as_issuer == RESOURCE_AS_ISSUER
        assert sent.resource_id == RESOURCE_ID
        assert sent.resource_as_token_url == RESOURCE_AS_TOKEN_URL

    @pytest.mark.asyncio
    async def test_sends_the_id_token_by_default(self) -> None:
        run = stub_id_jag()

        await apply_server_auth(
            {}, ema_server(), caller=alice(), obo_cache=OBOTokenCache(), id_jag=run
        )

        sent = run.seen[0]  # type: ignore[attr-defined]
        assert sent.subject_token == "alice-id-token"
        assert sent.subject_token_type.endswith("id_token")

    @pytest.mark.asyncio
    async def test_access_token_mode_sends_the_access_token(self) -> None:
        # ADR 0006: Keycloak refuses an ID Token as subject_token outright.
        run = stub_id_jag()

        await apply_server_auth(
            {},
            ema_server(ema_subject_token_type="access_token"),
            caller=alice(),
            obo_cache=OBOTokenCache(),
            id_jag=run,
        )

        sent = run.seen[0]  # type: ignore[attr-defined]
        assert sent.subject_token == "alice-access-token"
        assert sent.subject_token_type.endswith("access_token")

    @pytest.mark.asyncio
    async def test_records_success(self) -> None:
        server = ema_server()

        await apply_server_auth(
            {}, server, caller=alice(), obo_cache=OBOTokenCache(), id_jag=stub_id_jag()
        )

        assert server.ema_status == "ok"
        assert server.ema_error == ""


class TestFailsClosed:
    @pytest.mark.asyncio
    async def test_missing_id_token_is_refused_rather_than_downgraded(self) -> None:
        # Never silently switch to the access token: the two produce different IdP
        # policy evaluation, so the effective identity would depend on which attempt
        # happened to work (ADR 0006).
        headers: dict[str, str] = {}
        server = ema_server()
        run = stub_id_jag()

        with pytest.raises(OBOAuthError):
            await apply_server_auth(
                headers, server, caller=alice(id_token=""), obo_cache=OBOTokenCache(), id_jag=run
            )

        assert "Authorization" not in headers
        assert run.seen == []  # type: ignore[attr-defined]
        assert server.ema_status == "error"

    @pytest.mark.asyncio
    async def test_anonymous_caller_is_refused(self) -> None:
        with pytest.raises(OBOAuthError):
            await apply_server_auth(
                {},
                ema_server(),
                caller=Principal.anonymous(),
                obo_cache=OBOTokenCache(),
                id_jag=stub_id_jag(),
            )

    @pytest.mark.asyncio
    async def test_exchange_failure_never_falls_back_to_a_static_credential(self) -> None:
        headers: dict[str, str] = {}
        server = ema_server(bearer_token="a-static-fallback")

        async def failing(request, **kwargs):
            raise IDJagError("nope", leg=2, error="invalid_grant", error_description="rejected")

        with pytest.raises(OBOAuthError):
            await apply_server_auth(
                headers, server, caller=alice(), obo_cache=OBOTokenCache(), id_jag=failing
            )

        assert "Authorization" not in headers
        assert "leg 2" in server.ema_error

    @pytest.mark.asyncio
    async def test_missing_resource_as_configuration_is_refused(self) -> None:
        with pytest.raises(OBOAuthError, match="resource"):
            await apply_server_auth(
                {},
                ema_server(ema_resource_as_token_url=""),
                caller=alice(),
                obo_cache=OBOTokenCache(),
                id_jag=stub_id_jag(),
            )


class TestCacheIsolation:
    @pytest.mark.asyncio
    async def test_two_backends_behind_different_authorization_servers_do_not_share(
        self,
    ) -> None:
        cache = OBOTokenCache()
        first = stub_id_jag("token-for-a")
        second = stub_id_jag("token-for-b")

        headers_a: dict[str, str] = {}
        headers_b: dict[str, str] = {}
        await apply_server_auth(
            headers_a, ema_server(), caller=alice(), obo_cache=cache, id_jag=first
        )
        await apply_server_auth(
            headers_b,
            ema_server(id="other", ema_resource_as_issuer="https://other.example.com/oauth"),
            caller=alice(),
            obo_cache=cache,
            id_jag=second,
        )

        assert headers_a["Authorization"] == "Bearer token-for-a"
        assert headers_b["Authorization"] == "Bearer token-for-b"


class TestBackgroundPaths:
    @pytest.mark.asyncio
    async def test_service_identity_never_runs_the_flow(self) -> None:
        headers: dict[str, str] = {}
        run = stub_id_jag()

        await apply_server_auth(
            headers,
            ema_server(bearer_token="service-token"),
            caller=SERVICE_IDENTITY,
            obo_cache=OBOTokenCache(),
            id_jag=run,
        )

        assert run.seen == []  # type: ignore[attr-defined]
        assert headers["Authorization"] == "Bearer service-token"

    def test_ema_server_without_a_service_credential_needs_a_user(self) -> None:
        bare = ema_server(oauth_client_id="", oauth_client_secret="")

        assert needs_user_identity(bare) is True


class TestOtherServersAreUntouched:
    @pytest.mark.asyncio
    async def test_a_bearer_server_never_reaches_the_ema_path(self) -> None:
        headers: dict[str, str] = {}
        run = stub_id_jag()

        await apply_server_auth(
            headers,
            RegisteredServer(id="s", url="http://x", auth_type="bearer", bearer_token="tok"),
            caller=alice(),
            obo_cache=OBOTokenCache(),
            id_jag=run,
        )

        assert headers["Authorization"] == "Bearer tok"
        assert run.seen == []  # type: ignore[attr-defined]


class TestInvalidationIsFlowAware:
    @pytest.mark.asyncio
    async def test_invalidating_an_ema_entry_actually_clears_it(self) -> None:
        # A key built with the OBO shape would not match the EMA entry, so the
        # rejected token would stay cached until it expired -- the 401 retry would
        # re-send exactly the token the backend just refused.
        from mcp_hub.mcp.auth import invalidate_obo_token

        cache = OBOTokenCache()
        first = stub_id_jag("stale-token")
        server = ema_server()

        await apply_server_auth({}, server, caller=alice(), obo_cache=cache, id_jag=first)
        await invalidate_obo_token(server, alice(), obo_cache=cache)

        second = stub_id_jag("fresh-token")
        headers: dict[str, str] = {}
        await apply_server_auth(headers, server, caller=alice(), obo_cache=cache, id_jag=second)

        assert headers["Authorization"] == "Bearer fresh-token"
        assert len(second.seen) == 1  # type: ignore[attr-defined]
