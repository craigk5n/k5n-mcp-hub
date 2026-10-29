"""Run one tools/call against the REST API behind an OpenAPI server (Story 13.5).

The order is fixed: validate the arguments against the tool's own schema (before any
network I/O), build the request, apply the server's credentials **last** (so no
argument can override them), then send through the SSRF-pinned client.

- **Credentials** come only from `apply_server_auth`. The executor is never handed
  the inbound request, so nothing of the caller's -- their headers, cookies or
  token -- can reach the upstream. On-behalf-of and EMA exchange the calling
  `Principal`'s token, and fail closed (ADR 0003): if no token can be had, the API is
  not called.
- **Redirects** are followed only within the base URL's origin (scheme, host and
  port), up to `max_redirects`, and every hop is re-resolved and SSRF-checked before
  it is sent, in addition to the pinned transport's own check. A cross-origin
  redirect is refused, so the credential never goes there.
- **Responses** follow ADR 0011 §7, under one size cap and one deadline. Error text
  never includes the request URL, which can carry an API key in its query.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from mcp_hub.auth.caller import CallerIdentity
from mcp_hub.config import Settings
from mcp_hub.mcp.auth import OBOAuthError, apply_server_auth
from mcp_hub.models.server import RegisteredServer
from mcp_hub.openapi.arguments import is_json_media
from mcp_hub.openapi.model import Tool
from mcp_hub.openapi.request_builder import ArgumentProblem, PreparedRequest, build_request
from mcp_hub.openapi.validation import argument_error
from mcp_hub.trace.recorder import redact_query_params, sanitize_trace_headers
from mcp_hub.utils import resolve_pinned_ip, safe_http_client_factory

logger = logging.getLogger(__name__)

# Of an upstream error body, how much goes back to the caller.
_ERROR_BODY_CHARS = 2000
_DEFAULT_PORTS = {"http": 80, "https": 443}
_TEXT_MEDIA = ("application/xml", "application/x-www-form-urlencoded", "application/javascript")
_ACCEPT = "application/json, text/plain;q=0.9, */*;q=0.8"
# Decompression happens a chunk at a time before the size cap sees it; asking for
# none keeps that from mattering with any server that honours it.
_ACCEPT_ENCODING = "identity"
AuthApplier = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class UpstreamExchange:
    """One HTTP request to the REST API, for the trace recorder. Sanitized here,
    where the server's credentials are known: the URL has a query API key redacted,
    and the headers have Authorization and the API key header redacted."""

    method: str
    url: str
    status: int
    duration_ms: float
    request_headers: dict[str, str]
    response_headers: dict[str, str]
    request_body: bytes
    error: str = ""
    # The base URL plus the operation's path template: what a non-verbose trace
    # records, because the expanded URL carries the caller's argument values.
    url_template: str = ""


ExchangeObserver = Callable[[UpstreamExchange], None]


@dataclass(frozen=True)
class CallLimits:
    timeout_seconds: float
    max_response_bytes: int
    max_redirects: int
    allow_private_networks: bool

    @classmethod
    def from_settings(cls, settings: Settings) -> CallLimits:
        return cls(
            timeout_seconds=settings.openapi.call_timeout_seconds,
            max_response_bytes=settings.openapi.max_response_bytes,
            max_redirects=settings.openapi.max_redirects,
            allow_private_networks=settings.security.allow_private_networks,
        )


@dataclass(frozen=True)
class CallResult:
    """A tools/call result. `needs_authentication` marks an on-behalf-of failure whose
    fix is to authenticate, so the MCP layer can answer as the proxy does (401)."""

    content: tuple[dict[str, Any], ...]
    structured: dict[str, Any] | None = None
    is_error: bool = False
    needs_authentication: bool = False

    def to_mcp(self) -> dict[str, Any]:
        result: dict[str, Any] = {"content": list(self.content), "isError": self.is_error}
        if self.structured is not None:
            result["structuredContent"] = self.structured
        return result


def _text(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def _failure(text: str, *, needs_authentication: bool = False) -> CallResult:
    return CallResult((_text(text),), is_error=True, needs_authentication=needs_authentication)


async def call_tool(
    tool: Tool,
    arguments: dict[str, Any],
    *,
    server: RegisteredServer,
    caller: CallerIdentity,
    limits: CallLimits,
    http_client: httpx.AsyncClient | None = None,
    apply_auth: AuthApplier = apply_server_auth,
    on_exchange: ExchangeObserver | None = None,
) -> CallResult:
    if not server.openapi_base_url:
        # A draft (Story 13.8): imported, but no base URL confirmed yet.
        return _failure("This server has no base URL yet, so the call was not sent")
    problem = argument_error(tool.definition.input_schema, arguments)
    if problem is not None:
        return _failure(f"Invalid arguments: {problem}")
    try:
        prepared = build_request(tool.plan, arguments)
    except ArgumentProblem as exc:
        return _failure(f"Invalid arguments: {exc}")

    client = http_client or safe_http_client_factory(
        timeout=limits.timeout_seconds, allow_private_networks=limits.allow_private_networks
    )
    try:
        headers = httpx.Headers(
            {"Accept": _ACCEPT, "Accept-Encoding": _ACCEPT_ENCODING, "User-Agent": "k5n-mcp-hub"}
        )
        for name, value in prepared.headers:
            headers[name] = value
        if prepared.content_type:
            headers["Content-Type"] = prepared.content_type
        auth_params: dict[str, str] = {}
        try:
            await apply_auth(
                headers,
                server,
                caller=caller,
                params=auth_params,
                client=client,
                allow_private_networks=limits.allow_private_networks,
            )
        except OBOAuthError as exc:
            # Fail closed (ADR 0003). The detail is recorded on the server for the
            # admin; the caller is not handed the identity provider's text.
            return _failure(
                "Could not obtain a token for the upstream API, so the upstream API was not called",
                needs_authentication=exc.needs_authentication,
            )
        async with asyncio.timeout(limits.timeout_seconds):
            return await _send(
                client, tool, server, prepared, headers, auth_params, limits, on_exchange
            )
    except (TimeoutError, httpx.TimeoutException):
        return _failure(f"The upstream API timed out after {limits.timeout_seconds:g}s")
    except httpx.HTTPError as exc:
        # The class only: transport messages can include the URL, and with it an API
        # key sent in the query.
        logger.info("OpenAPI call to %s failed: %s", server.id, type(exc).__name__)
        return _failure(f"Could not reach the upstream API ({type(exc).__name__})")
    finally:
        if http_client is None:
            await client.aclose()


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme, url.host, url.port or _DEFAULT_PORTS.get(url.scheme)


def _with_auth_params(url: httpx.URL, auth_params: dict[str, str]) -> httpx.URL:
    """`url` with the API key's query parameter set -- and any same-named one from
    arguments or a redirect's Location replaced, never added alongside."""
    if not auth_params:
        return url
    # Case-insensitively: an upstream that reads `ApiKey` and `apikey` alike must not
    # see an argument's value next to the credential.
    taken = {name.lower() for name in auth_params}
    kept = [(k, v) for k, v in url.params.multi_items() if k.lower() not in taken]
    return url.copy_with(params=[*kept, *auth_params.items()])


async def _send(
    client: httpx.AsyncClient,
    tool: Tool,
    server: RegisteredServer,
    prepared: PreparedRequest,
    headers: httpx.Headers,
    auth_params: dict[str, str],
    limits: CallLimits,
    on_exchange: ExchangeObserver | None,
) -> CallResult:
    first = httpx.URL(server.openapi_base_url.rstrip("/") + prepared.path)
    origin = _origin(httpx.URL(server.openapi_base_url))
    if _origin(first) != origin:
        # Mapping refuses path templates that could do this; checked again here because
        # a request on the wrong host would carry the credential there.
        return _failure("The request URL is not on the base URL's origin, so it was not sent")
    url = _with_auth_params(first.copy_with(params=list(prepared.query)), auth_params)
    method, body = prepared.method, prepared.body
    for hop in range(limits.max_redirects + 1):
        if (
            await resolve_pinned_ip(url.host, allow_private_networks=limits.allow_private_networks)
            is None
        ):
            return _failure(
                f"The upstream host {url.host!r} failed SSRF validation, so it was not called"
            )
        request = client.build_request(method, url, headers=headers, content=body)
        started = time.perf_counter()
        try:
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            _observe(
                on_exchange,
                server,
                request,
                auth_params,
                started,
                0,
                {},
                type(exc).__name__,
                template=tool.plan.path,
            )
            raise
        _observe(
            on_exchange,
            server,
            request,
            auth_params,
            started,
            response.status_code,
            dict(response.headers),
            template=tool.plan.path,
        )
        try:
            location = response.headers.get("location")
            if not (300 <= response.status_code < 400 and location):
                return await _result(response, tool, server, limits)
            target = url.join(location)
            if target.userinfo:
                # httpx would turn userinfo into its own Authorization header, replacing
                # the server's credential with an identity the upstream chose.
                return _failure(
                    "The upstream API redirected to a URL with credentials in it; not followed"
                )
            if _origin(target) != origin:
                shown = redact_query_params(str(target), auth_params)
                return _failure(
                    f"The upstream API redirected to another origin ({shown}); not followed"
                )
            if hop == limits.max_redirects:
                return _failure(
                    f"The upstream API redirected more than {limits.max_redirects} times"
                )
            if response.status_code == 303 or (
                response.status_code in (301, 302) and method == "POST"
            ):
                method, body = "GET", None
                headers.pop("Content-Type", None)
            url = _with_auth_params(target, auth_params)
        finally:
            await response.aclose()
    raise AssertionError("unreachable: the loop returns on its last hop")


def _observe(
    on_exchange: ExchangeObserver | None,
    server: RegisteredServer,
    request: httpx.Request,
    auth_params: dict[str, str],
    started: float,
    status: int,
    response_headers: dict[str, str],
    error: str = "",
    *,
    template: str = "",
) -> None:
    if on_exchange is None:
        return
    extra = [server.api_key_name] if server.api_key_name else []
    on_exchange(
        UpstreamExchange(
            method=request.method,
            url=redact_query_params(str(request.url), auth_params),
            status=status,
            duration_ms=(time.perf_counter() - started) * 1000,
            request_headers=sanitize_trace_headers(dict(request.headers), extra_sensitive=extra),
            response_headers=sanitize_trace_headers(response_headers),
            request_body=request.content,
            error=error,
            url_template=server.openapi_base_url.rstrip("/") + template,
        )
    )


async def _read_capped(response: httpx.Response, limit: int) -> bytes | None:
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


def _decode(body: bytes, response: httpx.Response) -> str:
    # The charset is the upstream's to name; an unknown or broken one falls back to
    # UTF-8 rather than failing the call.
    try:
        return body.decode(response.charset_encoding or "utf-8", errors="replace")
    except (LookupError, ValueError):
        return body.decode("utf-8", errors="replace")


def _redact_secrets(text: str, server: RegisteredServer) -> str:
    """Upstream error bodies often echo the request, query string and all."""
    for secret in (server.api_key_value, server.bearer_token, server.basic_password):
        if secret and secret.strip():
            text = text.replace(secret.strip(), "****")
    return text


async def _result(
    response: httpx.Response, tool: Tool, server: RegisteredServer, limits: CallLimits
) -> CallResult:
    body = await _read_capped(response, limits.max_response_bytes)
    if body is None:
        return _failure(
            f"The upstream response was larger than {limits.max_response_bytes} bytes "
            "(openapi.max_response_bytes)"
        )
    plan = tool.plan
    if not response.is_success:
        detail = _redact_secrets(_decode(body, response), server)[:_ERROR_BODY_CHARS]
        summary = (
            f"The upstream API returned HTTP {response.status_code} for {plan.method} {plan.path}"
        )
        return _failure(f"{summary}: {detail}" if detail else summary)
    if not body:
        return CallResult((_text(f"HTTP {response.status_code} (no content)"),))
    media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if is_json_media(media):
        return _json_result(body, response, tool)
    if media.startswith("text/") or media in _TEXT_MEDIA or media.endswith("+xml"):
        return CallResult((_text(_decode(body, response)),))
    blob = {
        "uri": f"openapi://{server.id}{plan.path}",
        "mimeType": media or "application/octet-stream",
        "blob": base64.b64encode(body).decode("ascii"),
    }
    return CallResult(({"type": "resource", "resource": blob},))


def _json_result(body: bytes, response: httpx.Response, tool: Tool) -> CallResult:
    try:
        value = json.loads(body)
    except ValueError:
        return CallResult((_text(_decode(body, response)),))
    text = json.dumps(value, ensure_ascii=False)
    output = tool.plan.output
    schema = tool.definition.output_schema
    if output == "none" or schema is None:
        return CallResult((_text(text),))
    candidate = value if output == "object" else {"result": value}
    # MCP requires structured content to match the declared outputSchema. An API that
    # strays from its own spec gets text only, rather than a result clients reject.
    if not isinstance(candidate, dict) or argument_error(schema, candidate) is not None:
        return CallResult((_text(text),))
    return CallResult((_text(text),), structured=candidate)
