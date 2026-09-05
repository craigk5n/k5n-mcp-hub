"""`auth_type: "obo"` end to end through apply_server_auth (Stories 6.3, 6.5).

Every other registration must take exactly today's path -- the OBO rule is gated on
an exact auth_type match.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from mcp_hub.auth.caller import SERVICE_IDENTITY
from mcp_hub.auth.principal import Principal
from mcp_hub.mcp.auth import OBOAuthError, apply_server_auth
from mcp_hub.mcp.obo_cache import OBOTokenCache
from mcp_hub.mcp.token_exchange import ExchangedToken, TokenExchangeError
from mcp_hub.models.register_request import RegisterRequest
from mcp_hub.models.server import RegisteredServer

TOKEN_URL = "https://idp.example.com/realms/mcp-hub/protocol/openid-connect/token"


def obo_server(**overrides: Any) -> RegisteredServer:
    fields: dict[str, Any] = {
        "id": "files",
        "url": "https://files.example.com/mcp",
        "auth_type": "obo",
        "oauth_token_url": TOKEN_URL,
        "oauth_client_id": "k5n-mcp-hub",
        "oauth_client_secret": "hub-secret",
        "obo_audience": "mcp-server-files",
    }
    fields.update(overrides)
    return RegisteredServer(**fields)


def alice() -> Principal:
    return Principal(
        subject="alice",
        issuer="https://idp.example.com",
        token="alice-access-token",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def stub_exchange(access_token: str = "downstream-token"):
    seen: list[Any] = []

    async def exchange(request, **kwargs):
        seen.append(request)
        return ExchangedToken(access_token=access_token, expires_in=300)

    exchange.seen = seen  # type: ignore[attr-defined]
    return exchange


class TestModel:
    def test_server_accepts_obo_configuration(self) -> None:
        server = obo_server(obo_resource="https://files.example.com/mcp", obo_scope="mcp:invoke")

        assert server.auth_type == "obo"
        assert server.obo_audience == "mcp-server-files"
        assert server.obo_actor_token_source == "none"

    def test_register_request_accepts_obo(self) -> None:
        request = RegisterRequest(
            id="files",
            url="https://files.example.com/mcp",
            auth_type="obo",
            obo_audience="mcp-server-files",
        )

        assert request.auth_type == "obo"
        assert request.obo_audience == "mcp-server-files"

    def test_sanitize_for_api_scrubs_the_error_detail(self) -> None:
        server = obo_server(obo_status="error", obo_error="invalid_target: audience not found")

        sanitized = server.sanitize_for_api()

        assert sanitized.obo_error == ""
        assert sanitized.obo_status == "error"

    def test_sanitize_for_persistence_clears_runtime_state(self) -> None:
        server = obo_server(obo_status="ok", obo_error="stale")

        persisted = server.sanitize_for_persistence()

        assert persisted.obo_status == ""
        assert persisted.obo_error == ""

    def test_no_user_token_is_ever_persisted(self) -> None:
        # The exchanged token lives only in the in-memory cache.
        server = obo_server(obo_status="ok")
        blob = json.dumps(server.sanitize_for_persistence().model_dump(mode="json"))

        assert "alice-access-token" not in blob
        assert "downstream-token" not in blob


class TestUserPath:
    @pytest.mark.asyncio
    async def test_exchanges_and_sets_the_authorization_header(self) -> None:
        headers: dict[str, str] = {}
        exchange = stub_exchange()

        await apply_server_auth(
            headers,
            obo_server(),
            caller=alice(),
            obo_cache=OBOTokenCache(),
            exchange=exchange,
        )

        assert headers["Authorization"] == "Bearer downstream-token"
        assert exchange.seen[0].subject_token == "alice-access-token"  # type: ignore[attr-defined]
        assert exchange.seen[0].audience == "mcp-server-files"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_does_not_forward_the_user_token_in_x_mcp_token(self) -> None:
        # The static-bearer path mirrors the token into X-MCP-Token for servers behind
        # Apache. A user-scoped token gets no such second copy.
        headers: dict[str, str] = {}

        await apply_server_auth(
            headers,
            obo_server(),
            caller=alice(),
            obo_cache=OBOTokenCache(),
            exchange=stub_exchange(),
        )

        assert "X-MCP-Token" not in headers

    @pytest.mark.asyncio
    async def test_records_success_status(self) -> None:
        server = obo_server()

        await apply_server_auth(
            {}, server, caller=alice(), obo_cache=OBOTokenCache(), exchange=stub_exchange()
        )

        assert server.obo_status == "ok"
        assert server.obo_error == ""

    @pytest.mark.asyncio
    async def test_actor_token_only_when_delegation_is_configured(self) -> None:
        exchange = stub_exchange()

        await apply_server_auth(
            {}, obo_server(), caller=alice(), obo_cache=OBOTokenCache(), exchange=exchange
        )

        assert exchange.seen[0].actor_token == ""  # type: ignore[attr-defined]


class TestFailsClosed:
    @pytest.mark.asyncio
    async def test_anonymous_caller_is_refused(self) -> None:
        headers: dict[str, str] = {}
        server = obo_server()

        with pytest.raises(OBOAuthError):
            await apply_server_auth(
                headers,
                server,
                caller=Principal.anonymous(),
                obo_cache=OBOTokenCache(),
                exchange=stub_exchange(),
            )

        assert "Authorization" not in headers
        assert server.obo_status == "error"

    @pytest.mark.asyncio
    async def test_caller_without_an_exchangeable_token_is_refused(self) -> None:
        # Basic auth names a caller but yields nothing to exchange.
        with pytest.raises(OBOAuthError):
            await apply_server_auth(
                {},
                obo_server(),
                caller=Principal(subject="admin"),
                obo_cache=OBOTokenCache(),
                exchange=stub_exchange(),
            )

    @pytest.mark.asyncio
    async def test_exchange_failure_never_falls_back_to_the_service_credential(self) -> None:
        # ADR 0003: falling back would run the call under the hub's broader identity
        # and look like success.
        headers: dict[str, str] = {}
        server = obo_server(bearer_token="a-static-fallback-token")

        async def failing(request, **kwargs):
            raise TokenExchangeError(
                "nope", error="invalid_target", error_description="Audience not found"
            )

        with pytest.raises(OBOAuthError):
            await apply_server_auth(
                headers, server, caller=alice(), obo_cache=OBOTokenCache(), exchange=failing
            )

        assert "Authorization" not in headers
        assert server.obo_status == "error"
        assert "invalid_target" in server.obo_error

    @pytest.mark.asyncio
    async def test_missing_client_credentials_is_refused(self) -> None:
        with pytest.raises(OBOAuthError, match="client"):
            await apply_server_auth(
                {},
                obo_server(oauth_client_secret=""),
                caller=alice(),
                obo_cache=OBOTokenCache(),
                exchange=stub_exchange(),
            )


class TestBackgroundPathsKeepTheServiceIdentity:
    """Story 6.5 / ADR 0004."""

    @pytest.mark.asyncio
    async def test_service_identity_never_triggers_an_exchange(self) -> None:
        headers: dict[str, str] = {}
        exchange = stub_exchange()

        await apply_server_auth(
            headers,
            obo_server(bearer_token="service-token"),
            caller=SERVICE_IDENTITY,
            obo_cache=OBOTokenCache(),
            exchange=exchange,
        )

        assert exchange.seen == []  # type: ignore[attr-defined]
        assert headers["Authorization"] == "Bearer service-token"

    @pytest.mark.asyncio
    async def test_obo_server_without_a_service_credential_gets_no_header(self) -> None:
        # Degrades rather than failing: health can still probe reachability.
        headers: dict[str, str] = {}

        await apply_server_auth(
            headers,
            obo_server(),
            caller=SERVICE_IDENTITY,
            obo_cache=OBOTokenCache(),
            exchange=stub_exchange(),
        )

        assert "Authorization" not in headers


class TestOtherServersAreUntouched:
    @pytest.mark.asyncio
    async def test_bearer_server_never_reaches_the_obo_path(self) -> None:
        headers: dict[str, str] = {}
        exchange = stub_exchange()

        await apply_server_auth(
            headers,
            RegisteredServer(id="s", url="http://x", auth_type="bearer", bearer_token="tok"),
            caller=alice(),
            obo_cache=OBOTokenCache(),
            exchange=exchange,
        )

        assert headers["Authorization"] == "Bearer tok"
        assert headers["X-MCP-Token"] == "tok"
        assert exchange.seen == []  # type: ignore[attr-defined]


class TestServiceCredentialDetection:
    """Story 6.5: what the background paths can and cannot act as."""

    @pytest.mark.parametrize(
        "overrides",
        [
            {"bearer_token": "tok"},
            {"basic_username": "u", "basic_password": "p"},
            {},  # the obo_server fixture already carries oauth client credentials
        ],
        ids=["bearer", "basic", "client-credentials"],
    )
    def test_service_credential_is_detected(self, overrides: dict) -> None:
        from mcp_hub.mcp.auth import has_service_credential

        assert has_service_credential(obo_server(**overrides)) is True

    def test_obo_only_server_has_no_service_credential(self) -> None:
        from mcp_hub.mcp.auth import has_service_credential, needs_user_identity

        bare = obo_server(oauth_client_id="", oauth_client_secret="")

        assert has_service_credential(bare) is False
        assert needs_user_identity(bare) is True

    def test_non_obo_server_never_needs_a_user(self) -> None:
        from mcp_hub.mcp.auth import needs_user_identity

        assert needs_user_identity(RegisteredServer(id="s", url="http://x")) is False


class TestBackgroundDegradation:
    @pytest.mark.asyncio
    async def test_health_skips_the_mcp_probe_when_it_cannot_authenticate(self) -> None:
        # Probing would draw a 401 and mark a reachable server unhealthy.
        from unittest.mock import AsyncMock, MagicMock, patch

        import mcp_hub.health.checker as checker

        bare = obo_server(oauth_client_id="", oauth_client_secret="")
        instance = checker.HealthChecker.__new__(checker.HealthChecker)
        instance._allow_private_networks = False  # type: ignore[attr-defined]

        with patch.object(checker, "MCPClient") as mcp_client:
            with patch.object(checker, "StatelessMCPClient") as stateless:
                await checker.HealthChecker._mcp_probe(instance, bare)

        mcp_client.assert_not_called()
        stateless.assert_not_called()

    @pytest.mark.asyncio
    async def test_health_still_probes_when_a_service_credential_exists(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        import mcp_hub.health.checker as checker

        instance = checker.HealthChecker.__new__(checker.HealthChecker)
        instance._allow_private_networks = False  # type: ignore[attr-defined]

        with patch.object(checker, "MCPClient") as mcp_client:
            mcp_client.return_value.ping = AsyncMock()
            await checker.HealthChecker._mcp_probe(instance, obo_server(bearer_token="tok"))

        mcp_client.assert_called_once()

    @pytest.mark.asyncio
    async def test_discovery_skips_a_server_it_cannot_authenticate_to(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from mcp_hub.mcp.discovery import DiscoveryService

        bare = obo_server(oauth_client_id="", oauth_client_secret="")
        registry = MagicMock()
        registry.list = AsyncMock(return_value=[bare])

        service = DiscoveryService(registry)
        service.discover_immediately = AsyncMock()  # type: ignore[method-assign]
        await service.poll_once()

        service.discover_immediately.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_discovery_still_runs_with_a_service_credential(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from mcp_hub.mcp.discovery import DiscoveryService

        registry = MagicMock()
        registry.list = AsyncMock(return_value=[obo_server(bearer_token="tok")])

        service = DiscoveryService(registry)
        service.discover_immediately = AsyncMock()  # type: ignore[method-assign]
        await service.poll_once()

        service.discover_immediately.assert_awaited_once()  # type: ignore[attr-defined]
