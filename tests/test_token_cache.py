import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
import httpx

from mcp_hub.mcp.auth import TokenCache, DEFAULT_TOKEN_CACHE
from mcp_hub.models.server import RegisteredServer


def make_server(
    id: str = "test-id",
    url: str = "https://test.example.com",
    oauth_token_url: str = "https://idp.example.com/token",
    oauth_metadata: dict | None = None,
    oauth_client_id: str = "client-id",
    oauth_client_secret: str = "client-secret",
    oauth_resource: str = "",
    oauth_scope: str = "",
) -> RegisteredServer:
    return RegisteredServer(
        id=id,
        url=url,
        oauth_token_url=oauth_token_url,
        oauth_metadata=oauth_metadata,
        oauth_client_id=oauth_client_id,
        oauth_client_secret=oauth_client_secret,
        oauth_resource=oauth_resource,
        oauth_scope=oauth_scope,
    )


class TestTokenCache:
    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        self.cache = TokenCache()

    @pytest.mark.asyncio
    async def test_first_call_performs_post_second_call_returns_cached(
        self,
    ) -> None:
        server = make_server(
            oauth_token_url="https://idp.example.com/token",
            oauth_client_id="my-client",
            oauth_client_secret="my-secret",
        )

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "token-123",
            "expires_in": 600,
        }
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        token1 = await self.cache.token(server, client=mock_client)
        assert token1 == "token-123"
        assert mock_client.post.call_count == 1

        token2 = await self.cache.token(server, client=mock_client)
        assert token2 == "token-123"
        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_missing_oauth_token_url_and_empty_metadata_raises(
        self,
    ) -> None:
        server = RegisteredServer(
            id="test-id",
            url="https://test.example.com",
            oauth_token_url="",
            oauth_metadata=None,
            oauth_client_id="client-id",
            oauth_client_secret="client-secret",
        )

        with pytest.raises(RuntimeError, match="missing oauth token endpoint"):
            await self.cache.token(server)

    @pytest.mark.asyncio
    async def test_empty_oauth_client_id_raises(self) -> None:
        server = make_server(
            oauth_token_url="https://idp.example.com/token",
            oauth_client_id="",
            oauth_client_secret="secret",
        )

        with pytest.raises(RuntimeError, match="missing oauth client credentials"):
            await self.cache.token(server)

    @pytest.mark.asyncio
    async def test_empty_oauth_client_secret_raises(self) -> None:
        server = make_server(
            oauth_token_url="https://idp.example.com/token",
            oauth_client_id="client-id",
            oauth_client_secret="",
        )

        with pytest.raises(RuntimeError, match="missing oauth client credentials"):
            await self.cache.token(server)

    @pytest.mark.asyncio
    async def test_response_missing_access_token_raises(self) -> None:
        server = make_server(
            oauth_token_url="https://idp.example.com/token",
            oauth_client_id="my-client",
            oauth_client_secret="my-secret",
        )

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {}
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(RuntimeError, match="missing access_token"):
            await self.cache.token(server, client=mock_client)

    @pytest.mark.asyncio
    async def test_resource_and_scope_included_when_set(self) -> None:
        server = make_server(
            oauth_token_url="https://idp.example.com/token",
            oauth_client_id="my-client",
            oauth_client_secret="my-secret",
            oauth_resource="https://api.example.com",
            oauth_scope="read write",
        )

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "token-123"}
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        await self.cache.token(server, client=mock_client)

        call_args = mock_client.post.call_args
        assert call_args is not None
        _, kwargs = call_args
        assert kwargs["data"]["resource"] == "https://api.example.com"
        assert kwargs["data"]["scope"] == "read write"

    @pytest.mark.asyncio
    async def test_resource_and_scope_not_included_when_empty(self) -> None:
        server = make_server(
            oauth_token_url="https://idp.example.com/token",
            oauth_client_id="my-client",
            oauth_client_secret="my-secret",
            oauth_resource="",
            oauth_scope="",
        )

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "token-123"}
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        await self.cache.token(server, client=mock_client)

        call_args = mock_client.post.call_args
        assert call_args is not None
        _, kwargs = call_args
        assert "resource" not in kwargs["data"]
        assert "scope" not in kwargs["data"]

    @pytest.mark.asyncio
    async def test_token_url_from_metadata_when_oauth_token_url_empty(
        self,
    ) -> None:
        server = RegisteredServer(
            id="test-id",
            url="https://test.example.com",
            oauth_token_url="",
            oauth_metadata={"token_endpoint": "https://metadata.example.com/token"},
            oauth_client_id="client-id",
            oauth_client_secret="client-secret",
        )

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "token-123"}
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        await self.cache.token(server, client=mock_client)

        call_args = mock_client.post.call_args
        assert call_args is not None
        args, _ = call_args
        assert args[0] == "https://metadata.example.com/token"

    @pytest.mark.asyncio
    async def test_cache_key_uses_url_when_id_empty(self) -> None:
        server = RegisteredServer(
            id="",
            url="https://test.example.com",
            oauth_token_url="https://idp.example.com/token",
            oauth_client_id="client-id",
            oauth_client_secret="client-secret",
        )

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "token-123"}
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        await self.cache.token(server, client=mock_client)
        await self.cache.token(server, client=mock_client)

        assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_default_token_cache_is_singleton(self) -> None:
        assert DEFAULT_TOKEN_CACHE is not None
        assert isinstance(DEFAULT_TOKEN_CACHE, TokenCache)

    @pytest.mark.asyncio
    async def test_cache_uses_id_when_id_non_empty(self) -> None:
        server1 = make_server(id="server-1", url="https://a.example.com")
        server2 = make_server(id="server-2", url="https://a.example.com")

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "token-123"}
        mock_response.raise_for_status = AsyncMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_response)

        await self.cache.token(server1, client=mock_client)
        await self.cache.token(server2, client=mock_client)

        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_cache_refreshes_when_within_30_seconds(self) -> None:
        server = make_server(
            oauth_token_url="https://idp.example.com/token",
            oauth_client_id="my-client",
            oauth_client_secret="my-secret",
        )

        mock_response_1 = AsyncMock(spec=httpx.Response)
        mock_response_1.status_code = 200
        mock_response_1.json.return_value = {
            "access_token": "token-123",
            "expires_in": 60,
        }
        mock_response_1.raise_for_status = AsyncMock()

        mock_response_2 = AsyncMock(spec=httpx.Response)
        mock_response_2.status_code = 200
        mock_response_2.json.return_value = {
            "access_token": "token-456",
            "expires_in": 60,
        }
        mock_response_2.raise_for_status = AsyncMock()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(side_effect=[mock_response_1, mock_response_2])

        token1 = await self.cache.token(server, client=mock_client)
        assert token1 == "token-123"

        self.cache._tokens[server.id].expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=25
        )

        token2 = await self.cache.token(server, client=mock_client)
        assert token2 == "token-456"
        assert mock_client.post.call_count == 2
