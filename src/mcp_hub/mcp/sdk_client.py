from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError

from mcp_hub.auth.caller import CallerIdentity
from mcp_hub.mcp.auth import apply_server_auth
from mcp_hub.mcp.constants import METHOD_NOT_FOUND, resolve_protocol_version
from mcp_hub.mcp.pagination import collect_pages
from mcp_hub.models.server import RegisteredServer

if TYPE_CHECKING:
    from mcp.client.session import ClientSession


def _get_streamable_http_client() -> Any:
    """The SDK's streamable-HTTP transport factory.

    Indirected through a function purely so tests can patch it. `mcp` 2.x renamed
    `streamablehttp_client` to `streamable_http_client` within the same module; the
    pin in pyproject.toml keeps us on that side of the rename, so there is no
    fallback to maintain. (The old fallback named `mcp.client.streamablehttp`, a
    module that has never existed in either major version.)"""
    from mcp.client.streamable_http import streamable_http_client

    return streamable_http_client


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


logger = logging.getLogger(__name__)


def _find_validation_error(exc: BaseException) -> ValidationError | None:
    """The pydantic error inside `exc`, however deeply it is wrapped.

    anyio task groups raise ExceptionGroups and the SDK re-raises with __cause__
    chains, so the error we care about is rarely the one we are handed."""
    seen: set[int] = set()

    def walk(e: BaseException | None) -> ValidationError | None:
        if e is None or id(e) in seen:
            return None
        seen.add(id(e))
        if isinstance(e, ValidationError):
            return e
        for sub in getattr(e, "exceptions", None) or ():
            found = walk(sub)
            if found is not None:
                return found
        return walk(e.__cause__) or walk(e.__context__)

    return walk(exc)


def _normalize_schema_defects(items: list[dict], kind: str) -> tuple[list[dict], list[str]]:
    """Repair only defects whose correct form is unambiguous.

    Currently one: `"properties": []`. JSON Schema requires an object there, and PHP's
    json_encode emits `[]` for an empty associative array — a common enough server bug
    to be worth absorbing. An empty array and an empty object both mean "no
    properties", so the coercion cannot change a tool's meaning. Anything less
    clear-cut is left alone and simply reported.
    """
    issues: list[str] = []

    def repair(node: Any, path: str) -> Any:
        if isinstance(node, list):
            return [repair(v, f"{path}[{i}]") for i, v in enumerate(node)]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, value in node.items():
            where = f"{path}.{key}" if path else key
            if key == "properties" and value == []:
                issues.append(
                    f"{where} was [] (an empty JSON array); JSON Schema requires an "
                    "object here, so it was read as {}"
                )
                out[key] = {}
            else:
                out[key] = repair(value, where)
        return out

    repaired = [
        repair(item, str(item.get("name") or item.get("uri") or f"{kind}[{i}]"))
        for i, item in enumerate(items)
    ]
    return repaired, issues


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
        # Captured from the `mcp-session-id` response header (see _open_transport);
        # `mcp` 2.x no longer yields an accessor for it.
        self._session_id: str = ""
        # Defects tolerated while parsing this server's list responses. Discovery
        # copies these onto the server record so a repaired response is still
        # reported as non-conformant rather than passing silently.
        self.schema_issues: list[str] = []
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
            from mcp_hub.utils import safe_httpx2_client_factory

            # Keep the transport + session context managers open on an AsyncExitStack for
            # the client's whole lifetime, entered and closed in the SAME task (via
            # `async with MCPClient(...)`). The MCP SDK's streamable transport is an anyio
            # task group — entering it inside a nested `async with` and exiting it here
            # (while the session lives on) violates anyio's cancel-scope rules and raises
            # "Attempted to exit a cancel scope that isn't the current task's".
            self._exit_stack = AsyncExitStack()

            # `mcp` 2.x replaced the transport's `headers` and `httpx_client_factory`
            # arguments with a single `http_client`, so both the auth headers and the
            # SSRF pin now travel on a client we build. Losing either would be silent:
            # missing headers breaks authenticated servers, and an unpinned client
            # leaves every SDK call open to DNS rebinding.
            # 2.x also stopped yielding a session-id accessor, so capture the header
            # ourselves. Reading it off the response keeps the Initialize panel's
            # session display working without reaching into SDK internals.
            async def _capture_session_id(response: Any) -> None:
                sid = response.headers.get("mcp-session-id")
                if sid:
                    self._session_id = sid

            http_client = await self._exit_stack.enter_async_context(
                safe_httpx2_client_factory(
                    headers=headers,
                    allow_private_networks=self._allow_private_networks,
                    event_hooks={"response": [_capture_session_id]},
                )
            )
            transport = streamable_http_client(self.base_url, http_client=http_client)
            read_stream, write_stream = await self._exit_stack.enter_async_context(transport)

            from mcp.client.session import ClientSession

            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream=read_stream, write_stream=write_stream)
            )

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

            session_id = self._session_id

            # "sse" is this codebase's marker for the streamable-HTTP transport, not
            # for a plain SSE endpoint: see templates/_health_badge.html, which renders
            # it as "Transport: Streamable HTTP", and ui_downloads.py's
            # `is_streamable = srv.mcp_transport == "sse"`, which selects the streaming
            # script variant. "http" means plain non-streaming JSON. We open a
            # streamable-HTTP transport, so "sse" is correct here despite reading oddly.
            # (Changed to "http" in Story 4.1 on the mistaken belief that it was a bug;
            # that broke the badge and the generated scripts, and testing against a real
            # hosted server is what caught it.)
            self._transport_type = "sse"

            self._initialize_result = InitializeResult(
                server_name=result.server_info.name,
                server_version=result.server_info.version,
                protocol_version=str(result.protocol_version),
                session_id=session_id,
                transport=self._transport_type,
            )

            # `mcp` 2.x made ClientNotification a union type rather than a wrapper
            # model, so the notification object is sent directly.
            from mcp.types import InitializedNotification

            await self._session.send_notification(InitializedNotification())

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

    async def _list_leniently(
        self, method: str, attr: str, *, timeout: float, cause: ValidationError
    ) -> list[dict]:
        """Re-ask for the same list, bypassing the SDK's per-revision validation.

        `ClientSession.send_request` validates the response against the *negotiated
        revision's* wire model (`_methods.validate_server_result`) before it ever
        applies the caller's `result_type`, and catches only KeyError from it. So
        passing a laxer result model does not help: the strict check has already
        raised. One rung lower, `Dispatcher.send_raw_request` returns the result dict
        with no validation at all, which is exactly what we want here.

        `_dispatcher` is private API. It is used deliberately rather than re-issuing
        raw HTTP: the session already owns the transport, auth headers, session id and
        SSRF pin, and duplicating that would mean duplicating the streamable-HTTP and
        SSE handling too. If a future SDK drops the attribute, the fallback degrades to
        the original error rather than misreporting an empty capability list.
        """
        dispatcher = getattr(self._session, "_dispatcher", None)
        if dispatcher is None or not hasattr(dispatcher, "send_raw_request"):
            raise MCPClientError(
                f"List {method} failed: {_flatten_exc(cause)}",
                kind="list",
            ) from cause

        async def fetch(cursor: str | None) -> Any:
            params = {"cursor": cursor} if cursor else None
            return await asyncio.wait_for(
                dispatcher.send_raw_request(method, params, {"timeout": timeout}),
                timeout=timeout,
            )

        def merge(acc: Any, page: Any) -> Any:
            acc.setdefault(attr, []).extend((page or {}).get(attr, []))
            return acc

        raw = await collect_pages(fetch, merge=merge, label=f"{self.base_url} {method}")
        items = _as_dicts((raw or {}).get(attr, []))
        repaired, issues = _normalize_schema_defects(items, attr)

        summary = (
            f"{method} did not validate against the MCP schema for this protocol "
            f"revision: {cause.error_count()} error(s)"
        )
        for issue in [summary, *issues]:
            if issue not in self.schema_issues:
                self.schema_issues.append(issue)
        logger.warning(
            "%s returned a non-conformant %s; parsed it leniently (%d item(s)): %s",
            self.base_url,
            method,
            len(repaired),
            "; ".join(issues) or str(cause).splitlines()[0],
        )
        return repaired

    async def list(self, method: str, timeout: float = 30.0) -> Any:
        if not self._session:
            raise MCPClientError(
                "Client not initialized. Call handshake() first or use async context manager.",
                kind="list",
            )

        callers = {
            "tools/list": (lambda p: self._session.list_tools(params=p), "tools"),  # type: ignore[union-attr]
            "prompts/list": (lambda p: self._session.list_prompts(params=p), "prompts"),  # type: ignore[union-attr]
            "resources/list": (lambda p: self._session.list_resources(params=p), "resources"),  # type: ignore[union-attr]
        }

        try:
            if method in callers:
                call, attr = callers[method]

                async def fetch(cursor: str | None) -> Any:
                    params = None
                    if cursor:
                        from mcp.types import PaginatedRequestParams

                        params = PaginatedRequestParams(cursor=cursor)
                    return await asyncio.wait_for(call(params), timeout=timeout)

                def merge(acc: Any, page: Any) -> Any:
                    getattr(acc, attr).extend(getattr(page, attr, []))
                    return acc

                try:
                    result = await collect_pages(
                        fetch, merge=merge, label=f"{self.base_url} {method}"
                    )
                except Exception as strict_error:
                    # Only a schema-validation failure is recoverable this way. A
                    # transport error, timeout or protocol error must still surface:
                    # retrying those leniently would turn a broken connection into a
                    # confident empty capability list.
                    validation_error = _find_validation_error(strict_error)
                    if validation_error is None:
                        raise
                    return await self._list_leniently(
                        method, attr, timeout=timeout, cause=validation_error
                    )
                return _as_dicts(getattr(result, attr, result))
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
