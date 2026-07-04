from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal

from devhub.mcp.auth import apply_server_auth
from devhub.mcp.constants import PROTOCOL_VERSION
from devhub.models.server import RegisteredServer

if TYPE_CHECKING:
    from mcp.client.session import ClientSession


def _get_streamable_http_client() -> Any:
    try:
        from mcp.client.streamable_http import streamablehttp_client

        return streamablehttp_client
    except ImportError:
        pass

    try:
        from mcp.client.streamablehttp import streamable_http_client

        return streamable_http_client
    except ImportError:
        pass

    raise ImportError(
        "Could not find streamablehttp_client or streamable_http_client "
        "from mcp.client.streamable_http or mcp.client.streamablehttp"
    )


@dataclass
class InitializeResult:
    server_name: str
    server_version: str
    protocol_version: str
    session_id: str
    transport: Literal["http", "sse"]


class MCPClientError(Exception):
    def __init__(
        self,
        message: str,
        *,
        kind: Literal["handshake", "list", "ping", "transport"],
    ) -> None:
        super().__init__(message)
        self.kind = kind


class MCPClient:
    def __init__(
        self,
        base_url: str,
        *,
        server: RegisteredServer | None = None,
    ) -> None:
        self.base_url = base_url
        self.server = server
        self._session: ClientSession | None = None
        self._initialize_result: InitializeResult | None = None
        self._get_session_id: Callable[[], str | None] | None = None
        self._transport_type: Literal["http", "sse"] = "http"
        self._headers: dict[str, str] = {}

    @property
    def initialize_result(self) -> InitializeResult | None:
        return self._initialize_result

    async def __aenter__(self) -> "MCPClient":
        await self._open_transport()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self._close_transport()

    async def _open_transport(self) -> None:
        streamable_http_client = _get_streamable_http_client()

        headers: dict[str, str] = {}
        if self.server:
            await apply_server_auth(headers, self.server)

        if "MCP-Protocol-Version" not in headers:
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION

        self._headers = headers

        try:
            # Pin the outbound connection to a validated public IP (SSRF/DNS-rebinding
            # defense) when the installed MCP SDK supports a custom httpx client factory.
            import inspect

            from devhub.utils import safe_http_client_factory

            extra: dict[str, Any] = {}
            try:
                if "httpx_client_factory" in inspect.signature(streamable_http_client).parameters:
                    extra["httpx_client_factory"] = safe_http_client_factory
            except (TypeError, ValueError):
                pass
            transport = streamable_http_client(self.base_url, headers=headers, **extra)
            async with (
                transport as (
                    read_stream,
                    write_stream,
                    get_session_id,
                )
            ):
                from mcp.client.session import ClientSession

                self._session = ClientSession(
                    read_stream=read_stream,
                    write_stream=write_stream,
                )
                await self._session.__aenter__()

                if callable(get_session_id):
                    self._get_session_id = get_session_id

        except Exception as e:
            raise MCPClientError(
                f"Failed to open transport: {e}",
                kind="transport",
            ) from e

    async def _close_transport(self) -> None:
        if self._session:
            await self._session.__aexit__(None, None, None)
            self._session = None

    async def handshake(self, timeout: float = 30.0) -> InitializeResult:
        if not self._session:
            await self._open_transport()

        assert self._session is not None

        try:
            result = await asyncio.wait_for(
                self._session.initialize(),
                timeout=timeout,
            )

            session_id = ""
            if self._get_session_id:
                sid = self._get_session_id()
                if sid:
                    session_id = sid

            self._transport_type = "sse"

            self._initialize_result = InitializeResult(
                server_name=result.serverInfo.name,
                server_version=result.serverInfo.version,
                protocol_version=str(result.protocolVersion),
                session_id=session_id,
                transport=self._transport_type,
            )

            from mcp.types import ClientNotification, InitializedNotification

            await self._session.send_notification(
                ClientNotification(root=InitializedNotification())
            )

            return self._initialize_result

        except asyncio.TimeoutError as e:
            raise MCPClientError(
                f"Handshake timed out after {timeout}s",
                kind="handshake",
            ) from e
        except Exception as e:
            if isinstance(e, MCPClientError):
                raise
            raise MCPClientError(
                f"Handshake failed: {e}",
                kind="handshake",
            ) from e

    async def list(self, method: str, timeout: float = 30.0) -> Any:
        if not self._session:
            raise MCPClientError(
                "Client not initialized. Call handshake() first or use async context manager.",
                kind="list",
            )

        try:
            if method == "tools/list":
                result: Any = await asyncio.wait_for(
                    self._session.list_tools(),
                    timeout=timeout,
                )
                return result.tools if hasattr(result, "tools") else result
            if method == "prompts/list":
                result = await asyncio.wait_for(
                    self._session.list_prompts(),
                    timeout=timeout,
                )
                return result.prompts if hasattr(result, "prompts") else result
            if method == "resources/list":
                result = await asyncio.wait_for(
                    self._session.list_resources(),
                    timeout=timeout,
                )
                return result.resources if hasattr(result, "resources") else result
            raise MCPClientError(
                f"Unknown method: {method}. Must be one of: tools/list, prompts/list, resources/list",
                kind="list",
            )
        except asyncio.TimeoutError as e:
            raise MCPClientError(
                f"List {method} timed out after {timeout}s",
                kind="list",
            ) from e
        except MCPClientError:
            raise
        except Exception as e:
            raise MCPClientError(
                f"List {method} failed: {e}",
                kind="list",
            ) from e

    async def ping(self, timeout: float = 10.0) -> None:
        try:
            async with self.__class__(
                self.base_url,
                server=self.server,
            ) as client:
                await client.handshake(timeout=timeout)
        except MCPClientError as e:
            if e.kind == "transport":
                raise MCPClientError(
                    f"Ping failed: {e}",
                    kind="ping",
                ) from e
            raise MCPClientError(
                f"Ping failed: {e}",
                kind="ping",
            ) from e
        except Exception as e:
            raise MCPClientError(
                f"Ping failed: {e}",
                kind="ping",
            ) from e
