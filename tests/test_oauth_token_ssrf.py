"""The OAuth token-endpoint POST carries client credentials, so it must go through the
SSRF-pinned transport when it owns its HTTP client. These tests confirm the pin is applied
(a loopback token endpoint is rejected pre-connect unless allow_private_networks is set).
"""

from typing import Literal

import httpx
import pytest

from mcp_hub.mcp.auth import TokenCache, apply_server_auth
from mcp_hub.models.server import RegisteredServer
from mcp_hub.auth.caller import SERVICE_IDENTITY


def make_oauth_server(token_url: str) -> RegisteredServer:
    auth_type: Literal["oauth"] = "oauth"
    return RegisteredServer(
        id="ssrf-test",
        url="https://public.example.com/mcp",
        auth_type=auth_type,
        oauth_token_url=token_url,
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
    )


@pytest.mark.asyncio
async def test_token_endpoint_loopback_blocked_by_default() -> None:
    # No client passed -> TokenCache builds its own SafePinnedTransport client. With
    # allow_private_networks=False, a loopback token endpoint fails SSRF validation
    # BEFORE any bytes leave, so the credentials never reach 127.0.0.1.
    cache = TokenCache()
    server = make_oauth_server("http://127.0.0.1:9/token")

    with pytest.raises(httpx.ConnectError, match="SSRF validation"):
        await cache.token(server, allow_private_networks=False)


@pytest.mark.asyncio
async def test_token_endpoint_ip_literal_metadata_host_blocked_by_default() -> None:
    # Cloud metadata endpoint is a classic SSRF target; it must be rejected pre-connect.
    cache = TokenCache()
    server = make_oauth_server("http://169.254.169.254/latest/token")

    with pytest.raises(httpx.ConnectError, match="SSRF validation"):
        await cache.token(server, allow_private_networks=False)


@pytest.mark.asyncio
async def test_apply_server_auth_records_ssrf_block_as_token_error() -> None:
    # apply_server_auth swallows the fetch failure into oauth_token_status/error rather
    # than raising, so a blocked private token endpoint surfaces as an error, not a token.
    server = make_oauth_server("http://127.0.0.1:9/token")
    headers: dict[str, str] = {}

    await apply_server_auth(headers, server, caller=SERVICE_IDENTITY, allow_private_networks=False)

    assert "Authorization" not in headers
    assert server.oauth_token_status == "error"
    assert "SSRF validation" in server.oauth_token_error


@pytest.mark.asyncio
async def test_token_endpoint_loopback_allowed_when_flag_set() -> None:
    # With allow_private_networks=True the pin permits loopback, so we get PAST SSRF
    # validation and fail only on the actual connection (nothing is listening on :9).
    cache = TokenCache()
    server = make_oauth_server("http://127.0.0.1:9/token")

    with pytest.raises(httpx.ConnectError) as exc_info:
        await cache.token(server, allow_private_networks=True)

    assert "SSRF validation" not in str(exc_info.value)
