import base64
import pytest
from typing import Literal
from unittest.mock import AsyncMock
import httpx

from mcp_hub.mcp.auth import TokenCache, DEFAULT_TOKEN_CACHE, apply_server_auth
from mcp_hub.models.server import RegisteredServer
from mcp_hub.auth.caller import SERVICE_IDENTITY


def make_server(
    id: str = "test-id",
    url: str = "https://test.example.com",
    bearer_token: str = "",
    basic_username: str = "",
    basic_password: str = "",
    auth_type: str = "",
    oauth_token_url: str = "",
    oauth_metadata: dict | None = None,
    oauth_client_id: str = "",
    oauth_client_secret: str = "",
) -> RegisteredServer:
    auth_type_value: Literal["bearer", "basic", "oauth", ""] = auth_type  # type: ignore[assignment]
    return RegisteredServer(
        id=id,
        url=url,
        bearer_token=bearer_token,
        basic_username=basic_username,
        basic_password=basic_password,
        auth_type=auth_type_value,
        oauth_token_url=oauth_token_url,
        oauth_metadata=oauth_metadata,
        oauth_client_id=oauth_client_id,
        oauth_client_secret=oauth_client_secret,
    )


class TestApplyServerAuth:
    @pytest.mark.asyncio
    async def test_bearer_token_sets_authorization_and_clears_oauth_status(self) -> None:
        server = make_server(bearer_token="abc")
        headers: dict[str, str] = {}

        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY)

        assert headers.get("Authorization") == "Bearer abc"
        assert server.oauth_token_status == ""
        assert server.oauth_token_error == ""

    @pytest.mark.asyncio
    async def test_bearer_token_with_whitespace_stripped(self) -> None:
        server = make_server(bearer_token="  abc  ")
        headers: dict[str, str] = {}

        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY)

        assert headers.get("Authorization") == "Bearer abc"

    @pytest.mark.asyncio
    async def test_bearer_token_empty_after_strip_does_not_set_auth(self) -> None:
        server = make_server(bearer_token="   ")
        headers: dict[str, str] = {}

        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY)

        assert "Authorization" not in headers

    @pytest.mark.asyncio
    async def test_basic_auth_sets_authorization_and_clears_oauth_status(self) -> None:
        server = make_server(auth_type="basic", basic_username="admin", basic_password="hunter2")
        headers: dict[str, str] = {}

        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY)

        expected = base64.b64encode(b"admin:hunter2").decode("ascii")
        assert headers.get("Authorization") == f"Basic {expected}"
        assert server.oauth_token_status == ""
        assert server.oauth_token_error == ""

    @pytest.mark.asyncio
    async def test_basic_auth_preserves_internal_spaces(self) -> None:
        # WordPress application passwords are displayed in space-separated groups and
        # are valid with the spaces intact — they must not be stripped.
        server = make_server(basic_username="admin", basic_password="TVBz CtEB XKhm 4F2A 42wO y47Y")
        headers: dict[str, str] = {}

        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY)

        expected = base64.b64encode(b"admin:TVBz CtEB XKhm 4F2A 42wO y47Y").decode("ascii")
        assert headers.get("Authorization") == f"Basic {expected}"

    @pytest.mark.asyncio
    async def test_basic_auth_strips_surrounding_whitespace(self) -> None:
        server = make_server(basic_username="  admin  ", basic_password="  secret\n")
        headers: dict[str, str] = {}

        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY)

        expected = base64.b64encode(b"admin:secret").decode("ascii")
        assert headers.get("Authorization") == f"Basic {expected}"

    @pytest.mark.asyncio
    async def test_basic_auth_does_not_set_x_mcp_token(self) -> None:
        server = make_server(basic_username="admin", basic_password="secret")
        headers: dict[str, str] = {}

        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY)

        assert "X-MCP-Token" not in headers

    @pytest.mark.asyncio
    async def test_basic_auth_empty_creds_does_not_set_auth(self) -> None:
        server = make_server(auth_type="basic", basic_username="  ", basic_password="")
        headers: dict[str, str] = {}

        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY)

        assert "Authorization" not in headers

    @pytest.mark.asyncio
    async def test_bearer_token_takes_precedence_over_basic(self) -> None:
        server = make_server(
            bearer_token="my-bearer-token",
            basic_username="admin",
            basic_password="secret",
        )
        headers: dict[str, str] = {}

        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY)

        assert headers.get("Authorization") == "Bearer my-bearer-token"

    @pytest.mark.asyncio
    async def test_oauth_with_valid_token_fetches_and_sets_authorization(
        self,
    ) -> None:
        server = make_server(
            auth_type="oauth",
            oauth_token_url="https://idp.example.com/token",
            oauth_client_id="client-id",
            oauth_client_secret="client-secret",
        )

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "oauth-token-123"}
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        headers: dict[str, str] = {}
        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY, client=mock_client)

        assert headers.get("Authorization") == "Bearer oauth-token-123"
        assert server.oauth_token_status == "ok"
        assert server.oauth_token_error == ""

    @pytest.mark.asyncio
    async def test_oauth_token_url_set_triggers_oauth_flow(self) -> None:
        server = make_server(
            oauth_token_url="https://idp.example.com/token",
            oauth_client_id="client-id",
            oauth_client_secret="client-secret",
        )

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "token-from-url"}
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        headers: dict[str, str] = {}
        cache = TokenCache()
        await apply_server_auth(
            headers, server, caller=SERVICE_IDENTITY, token_cache=cache, client=mock_client
        )

        assert headers.get("Authorization") == "Bearer token-from-url"
        assert server.oauth_token_status == "ok"

    @pytest.mark.asyncio
    async def test_oauth_metadata_set_triggers_oauth_flow(self) -> None:
        server = make_server(
            oauth_metadata={"token_endpoint": "https://metadata.example.com/token"},
            oauth_client_id="client-id",
            oauth_client_secret="client-secret",
        )

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "token-from-metadata"}
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        headers: dict[str, str] = {}
        cache = TokenCache()
        await apply_server_auth(
            headers, server, caller=SERVICE_IDENTITY, token_cache=cache, client=mock_client
        )

        assert headers.get("Authorization") == "Bearer token-from-metadata"
        assert server.oauth_token_status == "ok"

    @pytest.mark.asyncio
    async def test_oauth_token_fetch_raises_sets_error_status(self) -> None:
        server = make_server(
            auth_type="oauth",
            oauth_token_url="https://idp.example.com/token",
            oauth_client_id="client-id",
            oauth_client_secret="client-secret",
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=httpx.HTTPError("connection failed"))

        headers: dict[str, str] = {}
        cache = TokenCache()
        await apply_server_auth(
            headers, server, caller=SERVICE_IDENTITY, token_cache=cache, client=mock_client
        )

        assert "Authorization" not in headers
        assert server.oauth_token_status == "error"
        assert server.oauth_token_error == "connection failed"

    @pytest.mark.asyncio
    async def test_oauth_empty_token_response_sets_error_status(self) -> None:
        server = make_server(
            auth_type="oauth",
            oauth_token_url="https://idp.example.com/token",
            oauth_client_id="client-id",
            oauth_client_secret="client-secret",
        )

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        headers: dict[str, str] = {}
        cache = TokenCache()
        await apply_server_auth(
            headers, server, caller=SERVICE_IDENTITY, token_cache=cache, client=mock_client
        )

        assert "Authorization" not in headers
        assert server.oauth_token_status == "error"
        assert "missing access_token" in server.oauth_token_error

    @pytest.mark.asyncio
    async def test_no_auth_fields_means_headers_unchanged(self) -> None:
        server = make_server()
        headers: dict[str, str] = {"Existing": "header"}

        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY)

        assert "Authorization" not in headers
        assert headers == {"Existing": "header"}

    @pytest.mark.asyncio
    async def test_bearer_token_takes_precedence_over_oauth(self) -> None:
        server = make_server(
            bearer_token="my-bearer-token",
            auth_type="oauth",
            oauth_token_url="https://idp.example.com/token",
            oauth_client_id="client-id",
            oauth_client_secret="client-secret",
        )

        mock_client = AsyncMock(spec=httpx.AsyncClient)

        headers: dict[str, str] = {}
        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY, client=mock_client)

        assert headers.get("Authorization") == "Bearer my-bearer-token"
        assert mock_client.post.call_count == 0

    @pytest.mark.asyncio
    async def test_works_with_httpx_headers(self) -> None:
        server = make_server(bearer_token="abc")
        headers = httpx.Headers()

        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY)

        assert headers.get("Authorization") == "Bearer abc"

    @pytest.mark.asyncio
    async def test_custom_token_cache_used(self) -> None:
        custom_cache = TokenCache()
        server = make_server(
            auth_type="oauth",
            oauth_token_url="https://idp.example.com/token",
            oauth_client_id="client-id",
            oauth_client_secret="client-secret",
        )

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "custom-cache-token"}
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        headers: dict[str, str] = {}
        await apply_server_auth(
            headers, server, caller=SERVICE_IDENTITY, token_cache=custom_cache, client=mock_client
        )

        assert headers.get("Authorization") == "Bearer custom-cache-token"
        assert server.oauth_token_status == "ok"
