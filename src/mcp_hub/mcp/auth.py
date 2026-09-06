import asyncio
import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Awaitable, Callable, cast

import httpx

from mcp_hub.auth.caller import CallerIdentity, ServiceIdentity
from mcp_hub.auth.principal import Principal
from mcp_hub.models.server import RegisteredServer
from mcp_hub.mcp.oauth import token_endpoint_from_metadata
from mcp_hub.mcp.id_jag import (
    ACCESS_TOKEN_TYPE,
    ID_TOKEN_TYPE,
    IDJagAccessToken,
    IDJagError,
    IDJagRequest,
    exchange_for_access_token,
)
from mcp_hub.mcp.obo_cache import OBOCacheKey, OBOTokenCache
from mcp_hub.mcp.token_exchange import (
    ExchangeRequest,
    ExchangedToken,
    TokenExchangeError,
    exchange_token,
)
from mcp_hub.utils import SafePinnedTransport

logger = logging.getLogger(__name__)


class OBOAuthError(Exception):
    """On-behalf-of authentication could not be applied.

    Raised rather than silently leaving the header unset, because the caller must
    fail the request closed (ADR 0003). Falling back to the server's static or
    client-credentials identity would run the call with the hub's broader rights and
    look, in the response, exactly like success.
    """

    def __init__(
        self, message: str, *, detail: str = "", needs_authentication: bool = False
    ) -> None:
        super().__init__(message)
        self.detail = detail or message
        # True when the fix is "authenticate", not "reconfigure" — the caller turns
        # that into a 401 pointing at the hub's protected-resource metadata, rather
        # than a 502 that tells the client nothing actionable.
        self.needs_authentication = needs_authentication


@dataclass
class _CachedToken:
    value: str
    expires_at: datetime


class TokenCache:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tokens: dict[str, _CachedToken] = {}

    async def token(
        self,
        server: RegisteredServer,
        *,
        client: httpx.AsyncClient | None = None,
        allow_private_networks: bool = False,
    ) -> str:
        cache_key = server.id if server.id else server.url

        async with self._lock:
            cached = self._tokens.get(cache_key)
            if cached:
                now = datetime.now(timezone.utc)
                remaining = (cached.expires_at - now).total_seconds()
                if remaining > 30:
                    return cached.value

        token_url = server.oauth_token_url or token_endpoint_from_metadata(server.oauth_metadata)
        if not token_url:
            raise RuntimeError("missing oauth token endpoint")

        if not server.oauth_client_id or not server.oauth_client_secret:
            raise RuntimeError("missing oauth client credentials")

        form_data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": server.oauth_client_id,
            "client_secret": server.oauth_client_secret,
        }
        if server.oauth_resource:
            form_data["resource"] = server.oauth_resource
        if server.oauth_scope:
            form_data["scope"] = server.oauth_scope

        own_client = client is None
        if own_client:
            # Credentials go out on this request, so it must be SSRF-safe: pin the
            # connection to a validated IP and never follow redirects (a 3xx to an
            # internal URL would leak the client_secret / access_token).
            client = httpx.AsyncClient(
                follow_redirects=False,
                transport=SafePinnedTransport(allow_private_networks=allow_private_networks),
            )

        http_client = cast(httpx.AsyncClient, client)

        try:
            response = await http_client.post(
                token_url,
                data=form_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                timeout=10.0,
            )
            response.raise_for_status()
            payload = response.json()
            access_token = payload.get("access_token")
            if not access_token:
                raise RuntimeError("missing access_token in token response")
            expires_in = payload.get("expires_in", 300)
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(expires_in, 30))

            async with self._lock:
                self._tokens[cache_key] = _CachedToken(value=access_token, expires_at=expires_at)

            return access_token
        finally:
            if own_client:
                await http_client.aclose()


DEFAULT_TOKEN_CACHE = TokenCache()
DEFAULT_OBO_CACHE = OBOTokenCache()

ExchangeFn = Callable[..., Awaitable[ExchangedToken]]
IDJagFn = Callable[..., Awaitable[IDJagAccessToken]]


async def apply_server_auth(
    headers: httpx.Headers | dict[str, str],
    server: RegisteredServer,
    *,
    caller: CallerIdentity,
    token_cache: TokenCache = DEFAULT_TOKEN_CACHE,
    obo_cache: OBOTokenCache = DEFAULT_OBO_CACHE,
    exchange: ExchangeFn = exchange_token,
    id_jag: IDJagFn = exchange_for_access_token,
    client: httpx.AsyncClient | None = None,
    allow_private_networks: bool = False,
) -> None:
    """Apply server authentication to the given headers.

    ``caller`` is required, never defaulted (ADR 0004): pass the request's
    ``Principal``, or ``SERVICE_IDENTITY`` for a background call with no user in
    scope. Nothing reads it yet — Epic 6's on-behalf-of rule is what consumes it —
    but making the choice explicit now means a later call site cannot silently
    inherit the service identity for a user's request.

    Mutates the given headers in place to add an Authorization header where appropriate.
    Also mutates server.oauth_token_status and server.oauth_token_error; callers must
    persist the server after invocation when those change.

    Rules (first matching rule wins):
    1. If server.bearer_token is non-empty after strip -> set Authorization header;
       also clear oauth_token_status and oauth_token_error.
    2. Else if server.basic_username or server.basic_password is non-empty after strip
       -> set Authorization: Basic base64(user:pass); also clear oauth_token_status and
       oauth_token_error.
    3. Else if server.auth_type == 'oauth' OR server.oauth_token_url is set OR
       server.oauth_metadata is set -> attempt to fetch token from cache. On success:
       set Authorization header, set oauth_token_status='ok', oauth_token_error=''.
       On failure: leave Authorization unset, set oauth_token_status='error',
       oauth_token_error=str(exc) or 'empty token response'.
    4. Else: do not set an Authorization header.

    Rule 0 precedes all of these: when ``server.auth_type == "obo"`` and a user is in
    scope, the caller's token is exchanged (RFC 8693) and nothing else is consulted.
    A stale ``bearer_token`` left on an OBO server must not quietly disable per-user
    auth. Background callers (``SERVICE_IDENTITY``) skip rule 0 entirely and fall
    through to the rules below (ADR 0004).
    """
    if server.auth_type == "obo" and not isinstance(caller, ServiceIdentity):
        await _apply_obo_auth(
            headers,
            server,
            caller,
            token_cache=token_cache,
            obo_cache=obo_cache,
            exchange=exchange,
            client=client,
            allow_private_networks=allow_private_networks,
        )
        return

    if server.auth_type == "ema" and not isinstance(caller, ServiceIdentity):
        await _apply_ema_auth(
            headers,
            server,
            caller,
            obo_cache=obo_cache,
            id_jag=id_jag,
            client=client,
            allow_private_networks=allow_private_networks,
        )
        return

    token = server.bearer_token
    if token and token.strip():
        tok = token.strip()
        headers["Authorization"] = f"Bearer {tok}"
        # Also send X-MCP-Token: many MCP servers run behind Apache/PHP, which strips the
        # Authorization header unless specially configured. This fallback header (a common
        # MCP-server convention) reaches those servers; a server that only reads
        # Authorization simply ignores the extra header.
        headers["X-MCP-Token"] = tok
        server.oauth_token_status = ""
        server.oauth_token_error = ""
        return

    # Basic auth (e.g. WordPress Application Passwords). Strip leading/trailing
    # whitespace/newlines from pasted credentials, but preserve any internal spaces
    # (WP application passwords are displayed in space-separated groups and are valid
    # with the spaces intact). We base64-encode "user:pass" ourselves so a changed
    # username or password is always re-encoded correctly.
    username = server.basic_username.strip() if server.basic_username else ""
    password = server.basic_password.strip() if server.basic_password else ""
    if username or password:
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
        server.oauth_token_status = ""
        server.oauth_token_error = ""
        return

    if server.auth_type == "oauth" or server.oauth_token_url or server.oauth_metadata:
        try:
            token = await token_cache.token(
                server, client=client, allow_private_networks=allow_private_networks
            )
            if token:
                headers["Authorization"] = f"Bearer {token}"
                server.oauth_token_status = "ok"
                server.oauth_token_error = ""
            else:
                server.oauth_token_status = "error"
                server.oauth_token_error = "empty token response"
        except Exception as exc:  # noqa: BLE001
            server.oauth_token_status = "error"
            server.oauth_token_error = str(exc)


async def _apply_obo_auth(
    headers: httpx.Headers | dict[str, str],
    server: RegisteredServer,
    caller: CallerIdentity,
    *,
    token_cache: TokenCache,
    obo_cache: OBOTokenCache,
    exchange: ExchangeFn,
    client: httpx.AsyncClient | None,
    allow_private_networks: bool,
) -> None:
    """Exchange the caller's token for one bound to this backend, or fail closed."""
    if not isinstance(caller, Principal) or not caller.can_act_as_obo_subject():
        raise _obo_failure(
            server,
            "no user identity available to act on behalf of",
            needs_authentication=True,
        )

    token_url = server.oauth_token_url or token_endpoint_from_metadata(server.oauth_metadata)
    if not token_url:
        raise _obo_failure(server, "no oauth token endpoint configured")
    if not server.oauth_client_id or not server.oauth_client_secret:
        raise _obo_failure(server, "missing oauth client credentials for the exchange")

    actor_token = ""
    if server.obo_actor_token_source == "client_credentials":
        # Delegation: the hub presents its own token alongside the user's so the
        # issued token can name both parties (ADR 0002).
        try:
            actor_token = await token_cache.token(
                server, client=client, allow_private_networks=allow_private_networks
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as an OBO failure
            raise _obo_failure(server, f"could not obtain the hub's actor token: {exc}") from exc

    key = obo_cache_key(server, caller)

    async def fetch() -> ExchangedToken:
        return await exchange(
            ExchangeRequest(
                token_url=token_url,
                client_id=server.oauth_client_id,
                client_secret=server.oauth_client_secret,
                subject_token=caller.token,
                audience=server.obo_audience,
                resource=server.obo_resource,
                scope=server.obo_scope,
                actor_token=actor_token,
            ),
            client=client,
            allow_private_networks=allow_private_networks,
        )

    try:
        access_token = await obo_cache.token(key, fetch=fetch, subject_expires_at=caller.expires_at)
    except TokenExchangeError as exc:
        raise _obo_failure(server, exc.summary()) from exc

    # Only Authorization. The static-bearer path also mirrors the token into
    # X-MCP-Token for backends behind Apache, but a user-scoped token gets no second
    # copy — fewer places for it to be logged or leaked.
    headers["Authorization"] = f"Bearer {access_token}"
    server.obo_status = "ok"
    server.obo_error = ""


def _obo_failure(
    server: RegisteredServer, detail: str, *, needs_authentication: bool = False
) -> OBOAuthError:
    server.obo_status = "error"
    server.obo_error = detail
    logger.info("on-behalf-of auth failed for server %s: %s", server.id, detail)
    return OBOAuthError(
        f"on-behalf-of auth failed: {detail}",
        detail=detail,
        needs_authentication=needs_authentication,
    )


def has_service_credential(server: RegisteredServer) -> bool:
    """See ``RegisteredServer.has_service_credential`` — the logic lives on the model so
    templates can ask the same question."""
    return server.has_service_credential


def needs_user_identity(server: RegisteredServer) -> bool:
    return server.needs_user_identity


def obo_cache_key(server: RegisteredServer, caller: Principal) -> OBOCacheKey:
    """The cache entry for this caller and server, in whichever flow it uses.

    Flow-aware so invalidation cannot miss: an EMA entry is keyed on the resource
    authorization server, and looking it up with the OBO shape would silently fail to
    clear it, leaving a rejected token cached until it expired."""
    if server.auth_type == "ema":
        return OBOCacheKey(
            subject=caller.subject,
            issuer=caller.issuer,
            server_id=server.id or server.url,
            audience=server.ema_resource_as_issuer,
            scope=server.obo_scope,
            resource=server.ema_resource_id,
            # The subject-token choice changes which identity the IdP evaluates, so a
            # token fetched under one must not satisfy the other.
            flow=f"ema:{server.ema_subject_token_type}",
        )
    return OBOCacheKey(
        subject=caller.subject,
        issuer=caller.issuer,
        server_id=server.id or server.url,
        audience=server.obo_audience,
        scope=server.obo_scope,
        resource=server.obo_resource,
    )


async def invalidate_obo_token(
    server: RegisteredServer,
    caller: Principal,
    *,
    obo_cache: OBOTokenCache = DEFAULT_OBO_CACHE,
) -> None:
    """Drop this caller's cached token for this server.

    Used when the backend rejects a token that was previously good — the usual cause
    is rotation or revocation at the IdP, which one re-exchange fixes."""
    await obo_cache.invalidate(obo_cache_key(server, caller))


async def _apply_ema_auth(
    headers: httpx.Headers | dict[str, str],
    server: RegisteredServer,
    caller: CallerIdentity,
    *,
    obo_cache: OBOTokenCache,
    id_jag: IDJagFn,
    client: httpx.AsyncClient | None,
    allow_private_networks: bool,
) -> None:
    """Enterprise-Managed Authorization: ID-JAG at the IdP, then redeem it at the
    backend's own authorization server (ADR 0005)."""
    if not isinstance(caller, Principal) or caller.is_anonymous:
        raise _ema_failure(
            server, "no user identity available to act on behalf of", needs_authentication=True
        )

    subject_token, subject_token_type = _ema_subject_assertion(server, caller)

    idp_token_url = server.oauth_token_url or token_endpoint_from_metadata(server.oauth_metadata)
    if not idp_token_url:
        raise _ema_failure(server, "no enterprise IdP token endpoint configured")
    if not server.ema_resource_as_token_url or not server.ema_resource_as_issuer:
        raise _ema_failure(server, "no resource authorization server configured")
    if not server.oauth_client_id or not server.oauth_client_secret:
        raise _ema_failure(server, "missing oauth client credentials for the exchange")

    # Keyed on the resource AS, not the MCP server: two backends behind different
    # authorization servers must never share a cached token.
    key = obo_cache_key(server, caller)

    async def fetch() -> ExchangedToken:
        issued = await id_jag(
            IDJagRequest(
                idp_token_url=idp_token_url,
                resource_as_token_url=server.ema_resource_as_token_url,
                resource_as_issuer=server.ema_resource_as_issuer,
                resource_id=server.ema_resource_id,
                client_id=server.oauth_client_id,
                client_secret=server.oauth_client_secret,
                subject_token=subject_token,
                subject_token_type=subject_token_type,
                scope=server.obo_scope,
            ),
            client=client,
            allow_private_networks=allow_private_networks,
        )
        # The cache speaks ExchangedToken; both flows end in a bearer token with a
        # lifetime, so there is nothing flow-specific left to carry.
        return ExchangedToken(access_token=issued.access_token, expires_in=issued.expires_in)

    try:
        access_token = await obo_cache.token(key, fetch=fetch, subject_expires_at=caller.expires_at)
    except IDJagError as exc:
        raise _ema_failure(server, exc.summary()) from exc

    headers["Authorization"] = f"Bearer {access_token}"
    server.ema_status = "ok"
    server.ema_error = ""


def _ema_subject_assertion(server: RegisteredServer, caller: Principal) -> tuple[str, str]:
    """Pick what leg 1 sends, failing closed when it is absent.

    Never falls back to the other token type (ADR 0006): the two produce different
    IdP policy evaluation, so a silent switch would make the effective identity
    depend on which attempt happened to succeed."""
    if server.ema_subject_token_type == "access_token":
        if not caller.token:
            raise _ema_failure(
                server, "caller has no access token to exchange", needs_authentication=True
            )
        return caller.token, ACCESS_TOKEN_TYPE

    if not caller.id_token:
        raise _ema_failure(
            server,
            "caller supplied no identity assertion (ID token); the server is "
            "configured for ema_subject_token_type=id_token",
            needs_authentication=True,
        )
    return caller.id_token, ID_TOKEN_TYPE


def _ema_failure(
    server: RegisteredServer, detail: str, *, needs_authentication: bool = False
) -> OBOAuthError:
    server.ema_status = "error"
    server.ema_error = detail
    logger.info("enterprise-managed auth failed for server %s: %s", server.id, detail)
    return OBOAuthError(
        f"enterprise-managed auth failed: {detail}",
        detail=detail,
        needs_authentication=needs_authentication,
    )
