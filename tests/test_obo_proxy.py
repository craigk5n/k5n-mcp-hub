"""Fail-closed proxy behavior for on-behalf-of servers (Story 6.4, ADR 0003)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mcp_hub.auth.principal import Principal
from mcp_hub.config import AuthConfig, JWTAuthConfig, Settings, TraceConfig
from mcp_hub.mcp.auth import OBOAuthError
from mcp_hub.models.server import RegisteredServer
from mcp_hub.proxy.handler import proxy_request


def obo_server() -> RegisteredServer:
    return RegisteredServer(
        id="files",
        url="https://files.example.com",
        auth_type="obo",
        oauth_token_url="https://idp.example.com/token",
        oauth_client_id="k5n-mcp-hub",
        oauth_client_secret="hub-secret",
        obo_audience="mcp-server-files",
    )


def alice() -> Principal:
    return Principal(
        subject="alice",
        issuer="https://idp.example.com",
        token="alice-token",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def make_request(principal: object, *, auth_type: str = "jwt") -> MagicMock:
    settings = Settings(
        auth=AuthConfig(
            type=auth_type,
            jwt=JWTAuthConfig(
                issuer="https://idp.example.com",
                audience="k5n-mcp-hub",
                jwks_uri="https://idp.example.com/certs",
            ),
        )
    )
    request = MagicMock()
    request.headers = {"X-MCP-Target-Server": "files"}
    request.method = "POST"
    request.url = SimpleNamespace(path="/mcp", query=None)
    request.base_url = "http://testserver/"

    async def stream():
        yield b'{"jsonrpc":"2.0","method":"tools/list"}'

    request.stream = stream
    request.state = SimpleNamespace(principal=principal)
    request.app.state.settings = settings
    return request


def registry_with(server: RegisteredServer) -> MagicMock:
    registry = MagicMock()
    registry.get = AsyncMock(return_value=server)
    return registry


async def _empty_stream():
    if False:
        yield b""


class TestFailsClosed:
    @pytest.mark.asyncio
    async def test_exchange_failure_returns_502_without_calling_the_backend(self) -> None:
        server = obo_server()
        request = make_request(alice())

        with patch(
            "mcp_hub.proxy.handler.build_outbound_headers",
            side_effect=OBOAuthError("boom", detail="invalid_target: Audience not found"),
        ):
            with patch("httpx.AsyncClient") as backend:
                response = await proxy_request(
                    request, registry_with(server), MagicMock(), TraceConfig()
                )

        assert response.status_code == 502
        assert b"invalid_target" in response.body
        backend.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_identity_returns_401_pointing_at_metadata(self) -> None:
        # Actionable: the client learns where to authenticate instead of getting a
        # bare 502 about a configuration it cannot see.
        server = obo_server()
        request = make_request(Principal.anonymous())

        response = await proxy_request(request, registry_with(server), MagicMock(), TraceConfig())

        assert response.status_code == 401
        challenge = response.headers["WWW-Authenticate"]
        assert "resource_metadata=" in challenge
        assert ".well-known/oauth-protected-resource" in challenge

    @pytest.mark.asyncio
    async def test_no_challenge_header_when_the_hub_is_not_a_resource_server(self) -> None:
        request = make_request(Principal.anonymous(), auth_type="none")

        response = await proxy_request(
            request, registry_with(obo_server()), MagicMock(), TraceConfig()
        )

        assert response.status_code == 401
        assert "WWW-Authenticate" not in response.headers

    @pytest.mark.asyncio
    async def test_failure_is_recorded_in_the_trace(self) -> None:
        recorder = MagicMock()
        request = make_request(Principal.anonymous())

        await proxy_request(request, registry_with(obo_server()), recorder, TraceConfig())

        entry = recorder.add.call_args.args[0]
        assert entry.status == 401
        assert entry.error


class TestReExchangeOnBackend401:
    @pytest.mark.asyncio
    async def test_a_401_triggers_exactly_one_re_exchange(self) -> None:
        server = obo_server()
        request = make_request(alice())
        attempts: list[str] = []

        def stream_for(status: int):
            resp = MagicMock()
            resp.status_code = status
            resp.headers = {"content-type": "application/json"}
            resp.aiter_bytes = _empty_stream
            return resp

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def stream(self, **kwargs):
                attempts.append(kwargs["headers"].get("Authorization", ""))
                ctx = MagicMock()
                ctx.__aenter__ = AsyncMock(
                    return_value=stream_for(401 if len(attempts) == 1 else 200)
                )
                ctx.__aexit__ = AsyncMock(return_value=False)
                return ctx

        with patch("mcp_hub.proxy.handler.httpx.AsyncClient", return_value=FakeClient()):
            with patch(
                "mcp_hub.proxy.handler.build_outbound_headers",
                new_callable=AsyncMock,
                side_effect=[{"Authorization": "Bearer stale"}, {"Authorization": "Bearer fresh"}],
            ):
                with patch(
                    "mcp_hub.proxy.handler.invalidate_obo_token", new_callable=AsyncMock
                ) as invalidate:
                    response = await proxy_request(
                        request, registry_with(server), MagicMock(), TraceConfig()
                    )

        assert attempts == ["Bearer stale", "Bearer fresh"]
        invalidate.assert_awaited_once()
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_a_second_401_propagates_rather_than_looping(self) -> None:
        server = obo_server()
        request = make_request(alice())
        attempts: list[str] = []

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def stream(self, **kwargs):
                attempts.append("call")
                resp = MagicMock()
                resp.status_code = 401
                resp.headers = {
                    "content-type": "application/json",
                    "WWW-Authenticate": 'Bearer error="invalid_token"',
                }
                resp.aiter_bytes = _empty_stream
                ctx = MagicMock()
                ctx.__aenter__ = AsyncMock(return_value=resp)
                ctx.__aexit__ = AsyncMock(return_value=False)
                return ctx

        with patch("mcp_hub.proxy.handler.httpx.AsyncClient", return_value=FakeClient()):
            with patch(
                "mcp_hub.proxy.handler.build_outbound_headers",
                new_callable=AsyncMock,
                return_value={"Authorization": "Bearer whatever"},
            ):
                with patch("mcp_hub.proxy.handler.invalidate_obo_token", new_callable=AsyncMock):
                    response = await proxy_request(
                        request, registry_with(server), MagicMock(), TraceConfig()
                    )

        assert len(attempts) == 2  # never a third
        assert response.status_code == 401
        # The backend's own challenge survives, so the client sees why it was refused.
        assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'

    @pytest.mark.asyncio
    async def test_non_obo_server_401_is_not_retried(self) -> None:
        server = RegisteredServer(
            id="s", url="https://x.example", auth_type="bearer", bearer_token="tok"
        )
        request = make_request(alice())
        attempts: list[str] = []

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def stream(self, **kwargs):
                attempts.append("call")
                resp = MagicMock()
                resp.status_code = 401
                resp.headers = {"content-type": "application/json"}
                resp.aiter_bytes = _empty_stream
                ctx = MagicMock()
                ctx.__aenter__ = AsyncMock(return_value=resp)
                ctx.__aexit__ = AsyncMock(return_value=False)
                return ctx

        with patch("mcp_hub.proxy.handler.httpx.AsyncClient", return_value=FakeClient()):
            response = await proxy_request(
                request, registry_with(server), MagicMock(), TraceConfig()
            )

        assert len(attempts) == 1
        assert response.status_code == 401
