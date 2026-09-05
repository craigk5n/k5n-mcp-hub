import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import cast

import httpx

from mcp_hub.auth.caller import CallerIdentity
from mcp_hub.models.server import RegisteredServer
from mcp_hub.mcp.oauth import token_endpoint_from_metadata
from mcp_hub.utils import SafePinnedTransport


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


async def apply_server_auth(
    headers: httpx.Headers | dict[str, str],
    server: RegisteredServer,
    *,
    caller: CallerIdentity,
    token_cache: TokenCache = DEFAULT_TOKEN_CACHE,
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
    """
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
