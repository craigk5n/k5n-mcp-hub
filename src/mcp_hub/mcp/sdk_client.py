from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal

from mcp_hub.auth.caller import CallerIdentity
from mcp_hub.mcp.auth import apply_server_auth
from mcp_hub.mcp.constants import METHOD_NOT_FOUND, resolve_protocol_version
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
        kind: Literal["handshake", "list", "ping", "transport", "discover"],
        status_code: int | None = None,
        jsonrpc_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        # HTTP status of the underlying failure when one can be recovered (e.g. 429). Lets the
        # health checker tell "server rate-limited us" apart from "server is down".
        self.status_code = status_code
        # JSON-RPC error code when the server answered with an error object.
        self.jsonrpc_code = jsonrpc_code

    @property
    def is_method_not_found(self) -> bool:
        """The server answered but doesn't implement the method — the signal that a
        stateless ``server/discover`` probe should fall back to ``initialize``."""
        return self.jsonrpc_code == METHOD_NOT_FOUND or self.status_code in (404, 405)


# One MCP connection per server at a time. The streamable-HTTP transport issues several
# requests per session, and rate-limiting backends (e.g. WordPress) answer concurrent
# connections with 429 — which the SDK surfaces as an opaque BrokenResourceError / "unhandled
# errors in a TaskGroup". Serializing per base URL stops the hub from making a server
# rate-limit itself (health checker + discovery + capabilities fetch no longer collide).
# NOTE: the lock is non-reentrant, so never call ping() on an already-entered client for the
# same URL (the callers here don't).
_connection_locks: dict[str, asyncio.Lock] = {}


def _connection_lock(url: str) -> asyncio.Lock:
    lock = _connection_locks.get(url)
    if lock is None:
        lock = asyncio.Lock()
        _connection_locks[url] = lock
    return lock


def _extract_status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status from an exception tree. The streamable transport buries the
    real cause inside anyio ExceptionGroups and __cause__/__context__ chains; walk them for
    anything carrying a `.response.status_code` (httpx.HTTPStatusError) so callers can react to
    e.g. 429 Too Many Requests."""
    seen: set[int] = set()

    def walk(e: BaseException | None) -> int | None:
        if e is None or id(e) in seen:
            return None
        seen.add(id(e))
        code = getattr(getattr(e, "response", None), "status_code", None)
        if isinstance(code, int):
            return code
        for sub in getattr(e, "exceptions", None) or []:
            found = walk(sub)
            if found is not None:
                return found
        for chained in (getattr(e, "__cause__", None), getattr(e, "__context__", None)):
            found = walk(chained)
            if found is not None:
                return found
        return None

    return walk(exc)


def _flatten_exc(exc: BaseException) -> str:
    """Reduce an ExceptionGroup (anyio TaskGroup) to a meaningful message rather than the
    opaque 'unhandled errors in a TaskGroup (N sub-exceptions)'."""
    subs = getattr(exc, "exceptions", None)
    if subs:
        parts = [p for p in (_flatten_exc(s) for s in subs) if p]
        if parts:
            return "; ".join(dict.fromkeys(parts))
    msg = str(exc).strip()
    if msg:
        return msg
    if type(exc).__name__ == "BrokenResourceError":
        return "connection interrupted (the server may be rate-limiting concurrent requests)"
    return type(exc).__name__


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
        caller: CallerIdentity,
    ) -> None:
        self.base_url = base_url
        self._allow_private_networks = allow_private_networks
        self._caller = caller
        self.server = server
        self._session: ClientSession | None = None
        self._initialize_result: InitializeResult | None = None
        self._get_session_id: Callable[[], str | None] | None = None
        self._transport_type: Literal["http", "sse"] = "http"
        self._headers: dict[str, str] = {}
        self._exit_stack: AsyncExitStack | None = None
        self._conn_lock: asyncio.Lock | None = None

    @property
    def initialize_result(self) -> InitializeResult | None:
        return self._initialize_result

    async def __aenter__(self) -> "MCPClient":
        # Serialize connections to this server (see _connection_lock) so we never make it
        # rate-limit our own concurrent connections.
        self._conn_lock = _connection_lock(self.base_url)
        await self._conn_lock.acquire()
        try:
            await self._open_transport()
        except BaseException:
            self._conn_lock.release()
            self._conn_lock = None
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            await self._close_transport()
        finally:
            if self._conn_lock is not None:
                self._conn_lock.release()
                self._conn_lock = None

    async def _open_transport(self) -> None:
        streamable_http_client = _get_streamable_http_client()

        headers: dict[str, str] = {}
        if self.server:
            await apply_server_auth(
                headers,
                self.server,
                caller=self._caller,
                allow_private_networks=self._allow_private_networks,
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
                f"Failed to open transport: {_flatten_exc(e)}",
                kind="transport",
                status_code=_extract_status_code(e),
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
                f"Handshake failed: {_flatten_exc(e)}",
                kind="handshake",
                status_code=_extract_status_code(e),
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
                f"List {method} failed: {_flatten_exc(e)}",
                kind="list",
            ) from e

    async def ping(self, timeout: float = 10.0) -> None:
        try:
            # Reuse the caller's network policy. A local-first hub health-checks loopback/LAN
            # servers, so the ping's own connection must honor allow_private_networks too —
            # otherwise the SSRF pin blocks 127.0.0.1/LAN and a perfectly reachable local
            # server (e.g. a WordPress plugin on localhost) looks unreachable.
            async with self.__class__(
                self.base_url,
                server=self.server,
                allow_private_networks=self._allow_private_networks,
                caller=self._caller,
            ) as client:
                await client.handshake(timeout=timeout)
        except MCPClientError as e:
            raise MCPClientError(f"Ping failed: {e}", kind="ping", status_code=e.status_code) from e
        except Exception as e:
            # Flatten anyio ExceptionGroups ("unhandled errors in a TaskGroup") to the real
            # cause so health-check logs are actionable instead of opaque.
            raise MCPClientError(
                f"Ping failed: {_flatten_exc(e)}",
                kind="ping",
                status_code=_extract_status_code(e),
            ) from e
