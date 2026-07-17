from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal

from mcp_hub.mcp.auth import apply_server_auth
from mcp_hub.mcp.constants import resolve_protocol_version
from mcp_hub.models.server import RegisteredServer

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


def _as_dicts(items: Any) -> list[dict]:
    """Normalize SDK list results to plain dicts. Newer MCP SDKs return typed Pydantic
    models (Tool/Prompt/Resource); older ones (and the rest of this codebase) use dicts.
    Convert with by_alias so schema keys stay camelCase (e.g. ``inputSchema``)."""
    out: list[dict] = []
    for it in items or []:
        if hasattr(it, "model_dump"):
            out.append(it.model_dump(by_alias=True, mode="json"))
        elif isinstance(it, dict):
            out.append(it)
        else:
            out.append(dict(it))
    return out


class MCPClient:
    def __init__(
        self,
        base_url: str,
        *,
        server: RegisteredServer | None = None,
        allow_private_networks: bool = False,
    ) -> None:
        self.base_url = base_url
        self._allow_private_networks = allow_private_networks
        self.server = server
        self._session: ClientSession | None = None
        self._initialize_result: InitializeResult | None = None
        self._get_session_id: Callable[[], str | None] | None = None
        self._transport_type: Literal["http", "sse"] = "http"
        self._headers: dict[str, str] = {}
        self._exit_stack: AsyncExitStack | None = None

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
            await apply_server_auth(
                headers, self.server, allow_private_networks=self._allow_private_networks
            )

        if "MCP-Protocol-Version" not in headers:
            configured = self.server.mcp_protocol_version if self.server else None
            headers["MCP-Protocol-Version"] = resolve_protocol_version(configured)

        self._headers = headers

        try:
            # Pin the outbound connection to a validated public IP (SSRF/DNS-rebinding
            # defense) when the installed MCP SDK supports a custom httpx client factory.
            import inspect

            import functools

            from mcp_hub.utils import safe_http_client_factory

            pinned_factory = functools.partial(
                safe_http_client_factory,
                allow_private_networks=self._allow_private_networks,
            )
            extra: dict[str, Any] = {}
            try:
                if "httpx_client_factory" in inspect.signature(streamable_http_client).parameters:
                    extra["httpx_client_factory"] = pinned_factory
            except (TypeError, ValueError):
                pass
            # Keep the transport + session context managers open on an AsyncExitStack for
            # the client's whole lifetime, entered and closed in the SAME task (via
            # `async with MCPClient(...)`). The MCP SDK's streamable transport is an anyio
            # task group — entering it inside a nested `async with` and exiting it here
            # (while the session lives on) violates anyio's cancel-scope rules and raises
            # "Attempted to exit a cancel scope that isn't the current task's".
            self._exit_stack = AsyncExitStack()
            transport = streamable_http_client(self.base_url, headers=headers, **extra)
            read_stream, write_stream, get_session_id = await self._exit_stack.enter_async_context(
                transport
            )

            from mcp.client.session import ClientSession

            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream=read_stream, write_stream=write_stream)
            )

            if callable(get_session_id):
                self._get_session_id = get_session_id

        except Exception as e:
            if self._exit_stack is not None:
                await self._exit_stack.aclose()
                self._exit_stack = None
            self._session = None
            raise MCPClientError(
                f"Failed to open transport: {e}",
                kind="transport",
            ) from e

    async def _close_transport(self) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
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
                return _as_dicts(result.tools if hasattr(result, "tools") else result)
            if method == "prompts/list":
                result = await asyncio.wait_for(
                    self._session.list_prompts(),
                    timeout=timeout,
                )
                return _as_dicts(result.prompts if hasattr(result, "prompts") else result)
            if method == "resources/list":
                result = await asyncio.wait_for(
                    self._session.list_resources(),
                    timeout=timeout,
                )
                return _as_dicts(result.resources if hasattr(result, "resources") else result)
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
