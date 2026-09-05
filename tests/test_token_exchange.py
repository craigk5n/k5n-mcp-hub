"""RFC 8693 token exchange (Story 6.1).

The hub swaps the caller's access token for one whose audience is the downstream
MCP server. Per ADR 0002 the default request shape is impersonation -- what a stock
Keycloak's supported standard token exchange accepts -- with actor-token delegation
opt-in for issuers that implement it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from mcp_hub.mcp.token_exchange import (
    ACCESS_TOKEN_TYPE,
    TOKEN_EXCHANGE_GRANT_TYPE,
    ExchangeRequest,
    TokenExchangeError,
    exchange_token,
)

TOKEN_URL = "https://idp.example.com/realms/mcp-hub/protocol/openid-connect/token"


class Recorder:
    """Captures the exchange request and replies with a canned response."""

    def __init__(self, response: httpx.Response | None = None) -> None:
        self.response = response or httpx.Response(
            200, json={"access_token": "exchanged-token", "expires_in": 300}
        )
        self.form: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self.url = ""

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.url = str(request.url)
            self.headers = dict(request.headers)
            self.form = {
                key: values[0] for key, values in parse_qs(request.content.decode()).items()
            }
            return self.response

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def request_for(**overrides: Any) -> ExchangeRequest:
    fields: dict[str, Any] = {
        "token_url": TOKEN_URL,
        "client_id": "k5n-mcp-hub",
        "client_secret": "hub-secret",
        "subject_token": "alice-access-token",
    }
    fields.update(overrides)
    return ExchangeRequest(**fields)


class TestRequestShape:
    @pytest.mark.asyncio
    async def test_sends_the_token_exchange_grant(self) -> None:
        recorder = Recorder()

        await exchange_token(request_for(), client=recorder.client())

        assert recorder.form["grant_type"] == TOKEN_EXCHANGE_GRANT_TYPE
        assert recorder.form["subject_token"] == "alice-access-token"

    @pytest.mark.asyncio
    async def test_declares_the_subject_token_type(self) -> None:
        recorder = Recorder()

        await exchange_token(request_for(), client=recorder.client())

        assert recorder.form["subject_token_type"] == ACCESS_TOKEN_TYPE

    @pytest.mark.asyncio
    async def test_audience_is_sent_when_set(self) -> None:
        recorder = Recorder()

        await exchange_token(request_for(audience="mcp-server-files"), client=recorder.client())

        assert recorder.form["audience"] == "mcp-server-files"

    @pytest.mark.asyncio
    async def test_resource_indicator_is_sent_when_set(self) -> None:
        # RFC 8707: binds the issued token to one downstream resource.
        recorder = Recorder()

        await exchange_token(
            request_for(resource="https://files.example.com/mcp"), client=recorder.client()
        )

        assert recorder.form["resource"] == "https://files.example.com/mcp"

    @pytest.mark.asyncio
    async def test_scope_is_sent_when_set(self) -> None:
        recorder = Recorder()

        await exchange_token(request_for(scope="mcp:invoke"), client=recorder.client())

        assert recorder.form["scope"] == "mcp:invoke"

    @pytest.mark.asyncio
    async def test_omits_empty_optional_fields(self) -> None:
        recorder = Recorder()

        await exchange_token(request_for(), client=recorder.client())

        for field in ("audience", "resource", "scope"):
            assert field not in recorder.form


class TestDelegationIsOptIn:
    @pytest.mark.asyncio
    async def test_no_actor_token_by_default(self) -> None:
        # ADR 0002: Keycloak's supported standard token exchange has no actor_token
        # parameter at all, so the default shape must not send one.
        recorder = Recorder()

        await exchange_token(request_for(), client=recorder.client())

        assert "actor_token" not in recorder.form
        assert "actor_token_type" not in recorder.form

    @pytest.mark.asyncio
    async def test_actor_token_sent_when_delegation_configured(self) -> None:
        recorder = Recorder()

        await exchange_token(request_for(actor_token="hub-own-token"), client=recorder.client())

        assert recorder.form["actor_token"] == "hub-own-token"
        assert recorder.form["actor_token_type"] == ACCESS_TOKEN_TYPE


class TestResponse:
    @pytest.mark.asyncio
    async def test_returns_the_exchanged_token(self) -> None:
        recorder = Recorder(
            httpx.Response(
                200,
                json={
                    "access_token": "downstream-token",
                    "expires_in": 120,
                    "issued_token_type": ACCESS_TOKEN_TYPE,
                    "scope": "mcp:invoke",
                },
            )
        )

        result = await exchange_token(request_for(), client=recorder.client())

        assert result.access_token == "downstream-token"
        assert result.expires_in == 120
        assert result.scope == "mcp:invoke"

    @pytest.mark.asyncio
    async def test_missing_access_token_is_an_error(self) -> None:
        recorder = Recorder(httpx.Response(200, json={"expires_in": 300}))

        with pytest.raises(TokenExchangeError, match="access_token"):
            await exchange_token(request_for(), client=recorder.client())


class TestErrors:
    @pytest.mark.asyncio
    async def test_rfc6749_error_is_preserved(self) -> None:
        # "invalid_target" means the audience isn't configured at the IdP -- the single
        # most likely misconfiguration. Flattening it to "exchange failed" would waste
        # an operator's afternoon.
        recorder = Recorder(
            httpx.Response(
                400,
                json={
                    "error": "invalid_target",
                    "error_description": "Audience not found: mcp-server-files",
                },
            )
        )

        with pytest.raises(TokenExchangeError) as exc_info:
            await exchange_token(request_for(audience="mcp-server-files"), client=recorder.client())

        assert exc_info.value.error == "invalid_target"
        assert "Audience not found" in exc_info.value.error_description
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_non_json_error_still_raises_with_status(self) -> None:
        recorder = Recorder(httpx.Response(503, text="upstream down"))

        with pytest.raises(TokenExchangeError) as exc_info:
            await exchange_token(request_for(), client=recorder.client())

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_transport_failure_raises_token_exchange_error(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        client = httpx.AsyncClient(transport=httpx.MockTransport(boom))

        with pytest.raises(TokenExchangeError):
            await exchange_token(request_for(), client=client)

    def test_error_message_never_contains_token_material(self) -> None:
        error = TokenExchangeError(
            "exchange failed", error="invalid_grant", error_description="Token is not active"
        )

        assert "alice-access-token" not in str(error)


class TestSSRFSafety:
    def test_default_client_pins_and_refuses_redirects(self) -> None:
        # The subject token goes out on this request; a 3xx to an internal URL would
        # leak it, and the pin is what stops a rebind.
        from mcp_hub.mcp.token_exchange import build_exchange_client
        from mcp_hub.utils import SafePinnedTransport

        client = build_exchange_client()

        assert client.follow_redirects is False
        assert isinstance(client._transport, SafePinnedTransport)
