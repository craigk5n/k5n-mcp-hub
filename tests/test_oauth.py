import pytest
from unittest.mock import AsyncMock, patch
import httpx

from mcp_hub.mcp.oauth import (
    AuthChallenge,
    discover_oauth_metadata,
    format_auth_challenge,
    parse_www_authenticate,
    token_endpoint_from_metadata,
)
from mcp_hub.utils import SafePinnedTransport, resolve_pinned_ip, safe_http_client_factory


class TestSafeHttpClientFactory:
    """The MCP-compatible factory must be SSRF-safe by construction."""

    @pytest.mark.asyncio
    async def test_factory_disables_redirects_and_pins(self) -> None:
        client = safe_http_client_factory(headers={"X": "y"}, timeout=httpx.Timeout(5.0))
        try:
            assert client.follow_redirects is False
            assert isinstance(client._transport, SafePinnedTransport)
            assert client.headers["X"] == "y"
        finally:
            await client.aclose()


def _addrinfo(*ips: str):
    """Build a socket.getaddrinfo-shaped result for the given IPv4 addresses."""
    return [(2, 1, 6, "", (ip, 0)) for ip in ips]


class TestResolvePinnedIp:
    """DNS-rebinding defense: resolve+validate a host to a single public IP to pin to."""

    @pytest.mark.asyncio
    async def test_public_literal_ip_returned(self) -> None:
        assert await resolve_pinned_ip("93.184.216.34") == "93.184.216.34"

    @pytest.mark.asyncio
    async def test_private_literal_ip_rejected(self) -> None:
        assert await resolve_pinned_ip("127.0.0.1") is None
        assert await resolve_pinned_ip("10.0.0.5") is None
        assert await resolve_pinned_ip("169.254.169.254") is None  # cloud metadata

    @pytest.mark.asyncio
    async def test_hostname_resolving_public_is_pinned(self) -> None:
        with patch("mcp_hub.utils.socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            assert await resolve_pinned_ip("example.com") == "93.184.216.34"

    @pytest.mark.asyncio
    async def test_hostname_resolving_private_rejected(self) -> None:
        # Classic rebinding payload: a name that resolves to an internal address.
        with patch("mcp_hub.utils.socket.getaddrinfo", return_value=_addrinfo("127.0.0.1")):
            assert await resolve_pinned_ip("rebind.attacker.test") is None

    @pytest.mark.asyncio
    async def test_any_private_answer_rejects_whole_host(self) -> None:
        # If a host resolves to a mix, reject rather than pin to the public one.
        with patch(
            "mcp_hub.utils.socket.getaddrinfo", return_value=_addrinfo("93.184.216.34", "10.1.2.3")
        ):
            assert await resolve_pinned_ip("mixed.attacker.test") is None

    @pytest.mark.asyncio
    async def test_unresolvable_host_returns_none(self) -> None:
        import socket as _socket

        with patch("mcp_hub.utils.socket.getaddrinfo", side_effect=_socket.gaierror):
            assert await resolve_pinned_ip("nope.invalid") is None


class TestParseWwwAuthenticate:
    def test_empty_string_returns_none(self) -> None:
        result = parse_www_authenticate("")
        assert result is None

    def test_whitespace_only_returns_none(self) -> None:
        result = parse_www_authenticate("   ")
        assert result is None

    def test_bearer_with_error_and_scope(self) -> None:
        header = 'Bearer error="invalid_token", scope="read"'
        result = parse_www_authenticate(header)
        assert result is not None
        assert result.scheme == "Bearer"
        assert result.error == "invalid_token"
        assert result.scope == "read"
        assert result.raw == header

    def test_digest_scheme_no_parsed_params(self) -> None:
        header = 'Digest realm="x"'
        result = parse_www_authenticate(header)
        assert result is not None
        assert result.scheme == "Digest"
        assert result.error == ""
        assert result.scope == ""
        assert result.resource == ""
        assert result.raw == header

    def test_bearer_with_resource(self) -> None:
        header = 'Bearer resource="http://example.com"'
        result = parse_www_authenticate(header)
        assert result is not None
        assert result.scheme == "Bearer"
        assert result.resource == "http://example.com"

    def test_bearer_with_all_params(self) -> None:
        header = 'Bearer error="invalid_token", error_description="Token expired", scope="read write", resource="http://api.example.com"'
        result = parse_www_authenticate(header)
        assert result is not None
        assert result.scheme == "Bearer"
        assert result.error == "invalid_token"
        assert result.error_description == "Token expired"
        assert result.scope == "read write"
        assert result.resource == "http://api.example.com"

    def test_quoted_value_containing_comma_not_split(self) -> None:
        header = 'Bearer scope="read, write"'
        result = parse_www_authenticate(header)
        assert result is not None
        assert result.scheme == "Bearer"
        assert result.scope == "read, write"

    def test_bearer_only_scheme(self) -> None:
        header = "Bearer"
        result = parse_www_authenticate(header)
        assert result is not None
        assert result.scheme == "Bearer"
        assert result.raw == header

    def test_non_bearer_returns_raw(self) -> None:
        header = 'Basic realm="test"'
        result = parse_www_authenticate(header)
        assert result is not None
        assert result.scheme == "Basic"
        assert result.raw == header


class TestFormatAuthChallenge:
    def test_none_returns_empty_string(self) -> None:
        result = format_auth_challenge(None)
        assert result == ""

    def test_no_params_returns_raw(self) -> None:
        challenge = AuthChallenge(scheme="Bearer", raw='Bearer error="invalid"')
        result = format_auth_challenge(challenge)
        assert result == challenge.raw

    def test_error_with_description_and_scope(self) -> None:
        challenge = AuthChallenge(
            scheme="Bearer",
            error="invalid_token",
            error_description="Token expired",
            scope="read",
        )
        result = format_auth_challenge(challenge)
        assert result == "invalid_token: Token expired • scope=read"

    def test_only_error(self) -> None:
        challenge = AuthChallenge(scheme="Bearer", error="invalid_token")
        result = format_auth_challenge(challenge)
        assert result == "invalid_token"

    def test_only_scope(self) -> None:
        challenge = AuthChallenge(scheme="Bearer", scope="read")
        result = format_auth_challenge(challenge)
        assert result == "scope=read"

    def test_only_resource(self) -> None:
        challenge = AuthChallenge(scheme="Bearer", resource="http://api.example.com")
        result = format_auth_challenge(challenge)
        assert result == "resource=http://api.example.com"

    def test_all_params(self) -> None:
        challenge = AuthChallenge(
            scheme="Bearer",
            error="insufficient_scope",
            error_description="Need more permissions",
            scope="admin",
            resource="http://api.example.com",
        )
        result = format_auth_challenge(challenge)
        assert (
            result
            == "insufficient_scope: Need more permissions • scope=admin • resource=http://api.example.com"
        )


class TestDiscoverOAuthMetadata:
    @pytest.mark.asyncio
    async def test_discovery_url_used_when_provided(self) -> None:
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "issuer": "https://idp.example.com",
            "token_endpoint": "https://idp.example.com/token",
        }

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        used_url, issuer, metadata = await discover_oauth_metadata(
            "https://oauth.example.com",
            discovery_url="https://custom.example.com/.well-known/oauth-authorization-server",
            client=mock_client,
        )

        assert used_url == "https://custom.example.com/.well-known/oauth-authorization-server"
        assert issuer == "https://idp.example.com"
        assert metadata == {
            "issuer": "https://idp.example.com",
            "token_endpoint": "https://idp.example.com/token",
        }
        mock_client.get.assert_called_once()
        mock_client.get.assert_called_with(
            "https://custom.example.com/.well-known/oauth-authorization-server",
            headers={"Accept": "application/json"},
            timeout=10.0,
        )

    @pytest.mark.asyncio
    async def test_first_well_known_succeeds(self) -> None:
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"issuer": "https://idp.example.com"}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        used_url, issuer, metadata = await discover_oauth_metadata(
            "https://oauth.example.com",
            client=mock_client,
        )

        assert used_url == "https://oauth.example.com/.well-known/oauth-authorization-server"
        assert issuer == "https://idp.example.com"

    @pytest.mark.asyncio
    async def test_first_well_known_404_tries_second(self) -> None:
        mock_response_404 = AsyncMock(spec=httpx.Response)
        mock_response_404.status_code = 404

        mock_response_200 = AsyncMock(spec=httpx.Response)
        mock_response_200.status_code = 200
        mock_response_200.json.return_value = {
            "issuer": "https://openid.example.com",
            "token_endpoint": "https://openid.example.com/token",
        }

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=[mock_response_404, mock_response_200])

        used_url, issuer, metadata = await discover_oauth_metadata(
            "https://oauth.example.com",
            client=mock_client,
        )

        assert used_url == "https://oauth.example.com/.well-known/openid-configuration"
        assert issuer == "https://openid.example.com"
        assert mock_client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_all_candidates_fail_raises_runtime_error(self) -> None:
        mock_response_404 = AsyncMock(spec=httpx.Response)
        mock_response_404.status_code = 404

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response_404)

        with pytest.raises(RuntimeError, match="no oauth discovery document found"):
            await discover_oauth_metadata(
                "https://oauth.example.com",
                client=mock_client,
            )

    @pytest.mark.asyncio
    async def test_invalid_json_continues_to_next_candidate(self) -> None:
        mock_response_200 = AsyncMock(spec=httpx.Response)
        mock_response_200.status_code = 200
        mock_response_200.json.side_effect = ValueError("invalid json")

        mock_response_200_2 = AsyncMock(spec=httpx.Response)
        mock_response_200_2.status_code = 200
        mock_response_200_2.json.return_value = {"issuer": "https://valid.example.com"}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=[mock_response_200, mock_response_200_2])

        used_url, issuer, metadata = await discover_oauth_metadata(
            "https://oauth.example.com",
            client=mock_client,
        )

        assert used_url == "https://oauth.example.com/.well-known/openid-configuration"
        assert issuer == "https://valid.example.com"

    @pytest.mark.asyncio
    async def test_creates_and_closes_client_when_not_provided(self) -> None:
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"issuer": "https://idp.example.com"}

        with patch("mcp_hub.mcp.oauth.httpx.AsyncClient") as mock_client_class:
            mock_client_instance = AsyncMock()
            mock_client_instance.get = AsyncMock(return_value=mock_response)
            mock_client_instance.aclose = AsyncMock()
            mock_client_class.return_value = mock_client_instance

            used_url, issuer, metadata = await discover_oauth_metadata(
                "https://oauth.example.com",
            )

            assert used_url == "https://oauth.example.com/.well-known/oauth-authorization-server"
            assert issuer == "https://idp.example.com"
            mock_client_class.assert_called_once()
            mock_client_instance.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_https_scheme_derived_from_server_url(self) -> None:
        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"issuer": "https://idp.example.com"}

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        await discover_oauth_metadata(
            "https://oauth.example.com",
            client=mock_client,
        )

        call_args = mock_client.get.call_args
        assert call_args is not None
        assert "https://oauth.example.com/.well-known/oauth-authorization-server" in str(call_args)


class TestTokenEndpointFromMetadata:
    def test_with_valid_token_endpoint(self) -> None:
        metadata = {"token_endpoint": "https://x/t"}
        result = token_endpoint_from_metadata(metadata)
        assert result == "https://x/t"

    def test_with_none_returns_empty_string(self) -> None:
        result = token_endpoint_from_metadata(None)
        assert result == ""

    def test_with_empty_metadata_returns_empty_string(self) -> None:
        result = token_endpoint_from_metadata({})
        assert result == ""

    def test_with_whitespace_in_token_endpoint(self) -> None:
        metadata = {"token_endpoint": "  https://x/t  "}
        result = token_endpoint_from_metadata(metadata)
        assert result == "https://x/t"
