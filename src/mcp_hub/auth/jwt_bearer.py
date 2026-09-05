from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

import httpx
import jwt
from fastapi import Request

from mcp_hub.auth.principal import Principal
from mcp_hub.utils import safe_http_client_factory

logger = logging.getLogger(__name__)

# Asymmetric algorithms only. A JWKS publishes *public* keys, so an HMAC secret could
# never live in one safely, and permitting HS* is precisely the algorithm-confusion
# attack surface (re-sign a token with the public key as the shared secret).
DEFAULT_ALGORITHMS: tuple[str, ...] = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
)

_ASYMMETRIC_PREFIXES = ("RS", "PS", "ES", "ED")

# Hub-specific header. No MCP client sends this by convention; it is configuration
# between an operator and their own client (ADR 0006).
IDENTITY_ASSERTION_HEADER = "X-MCP-Identity-Assertion"

DEFAULT_MIN_REFRESH_INTERVAL_SECONDS = 60.0
JWKS_FETCH_TIMEOUT_SECONDS = 10.0


def scopes_from_claims(claims: Mapping[str, Any]) -> frozenset[str]:
    """Granted scopes, from either OAuth's space-delimited ``scope`` or the ``scp``
    array some issuers emit instead."""
    raw = claims.get("scope")
    if isinstance(raw, str):
        return frozenset(raw.split())

    scp = claims.get("scp")
    if isinstance(scp, str):
        return frozenset(scp.split())
    if isinstance(scp, list):
        return frozenset(str(item) for item in scp)

    return frozenset()


class JWKSCache:
    """The issuer's signing keys, fetched lazily and cached by ``kid``.

    Lazy on purpose (ADR 0001): fetching at startup would mean a merely-unreachable
    IdP stops the hub from booting. Unknown-kid refreshes are rate limited so a flood
    of bogus ``kid`` values can't turn the hub into a JWKS request amplifier.
    """

    def __init__(
        self,
        jwks_uri: str,
        *,
        allow_private_networks: bool = False,
        min_refresh_interval_seconds: float = DEFAULT_MIN_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self.jwks_uri = jwks_uri
        self._allow_private_networks = allow_private_networks
        self._min_refresh_interval = min_refresh_interval_seconds
        self._keys: dict[str, Any] = {}
        self._last_attempt = 0.0
        self._last_rotation_refresh = 0.0
        self._lock = asyncio.Lock()

    def build_client(self) -> httpx.AsyncClient:
        # JWKS is an outbound fetch like every other one here: pinned to a validated
        # IP, and never following a redirect that could bypass the pin.
        return safe_http_client_factory(
            timeout=httpx.Timeout(JWKS_FETCH_TIMEOUT_SECONDS),
            allow_private_networks=self._allow_private_networks,
        )

    async def key_for(self, kid: str | None, *, client: httpx.AsyncClient | None = None) -> Any:
        async with self._lock:
            key = self._lookup(kid)
            if key is not None:
                return key
            if not self._may_refresh():
                logger.debug("unknown kid %r and refresh is rate limited", kid)
                return None
            await self._refresh(client)
            return self._lookup(kid)

    def _lookup(self, kid: str | None) -> Any:
        if kid:
            return self._keys.get(kid)
        # A token carrying no `kid` is only resolvable when the JWKS advertises exactly
        # one key; with several we would be guessing which one signed it.
        if len(self._keys) == 1:
            return next(iter(self._keys.values()))
        return None

    def _may_refresh(self) -> bool:
        now = time.monotonic()
        if self._last_attempt == 0.0:
            return True
        if not self._keys:
            # Nothing cached yet — keep retrying, but no faster than the cooldown so a
            # down IdP isn't hammered.
            return (now - self._last_attempt) >= self._min_refresh_interval
        if self._last_rotation_refresh == 0.0:
            # First rotation refresh after a good load is always allowed: this is the
            # normal key-rotation path, not an attack.
            return True
        return (now - self._last_rotation_refresh) >= self._min_refresh_interval

    async def _refresh(self, client: httpx.AsyncClient | None) -> None:
        had_keys = bool(self._keys)
        own_client = client is None
        http_client = client if client is not None else self.build_client()
        document: Any = None
        try:
            response = await http_client.get(self.jwks_uri)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("JWKS fetch from %s failed: %s", self.jwks_uri, exc)
        finally:
            if own_client:
                await http_client.aclose()
            self._last_attempt = time.monotonic()
            if had_keys:
                self._last_rotation_refresh = self._last_attempt

        if isinstance(document, dict):
            parsed = self._parse(document)
            if parsed:
                self._keys = parsed

    @staticmethod
    def _parse(document: Mapping[str, Any]) -> dict[str, Any]:
        keys: dict[str, Any] = {}
        entries = document.get("keys")
        if not isinstance(entries, list):
            return keys

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            candidate = dict(entry)
            # Some issuers omit `alg`; PyJWK needs one to pick a algorithm class.
            if "alg" not in candidate and candidate.get("kty") == "RSA":
                candidate["alg"] = "RS256"
            try:
                parsed_key = jwt.PyJWK.from_dict(candidate).key
            except Exception as exc:  # noqa: BLE001 - one bad entry must not void the set
                logger.debug("skipping unusable JWKS entry %r: %s", candidate.get("kid"), exc)
                continue
            keys[str(candidate.get("kid") or "")] = parsed_key

        return keys


class JWTBearerStrategy:
    """Validates inbound OAuth access tokens against the issuer's JWKS.

    Returns a ``Principal`` carrying the raw token, because Epic 6 sends it back to
    the IdP as RFC 8693 ``subject_token``.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_uri: str,
        algorithms: Iterable[str] = DEFAULT_ALGORITHMS,
        required_scopes: frozenset[str] = frozenset(),
        leeway_seconds: float = 0.0,
        allow_private_networks: bool = False,
        min_refresh_interval_seconds: float = DEFAULT_MIN_REFRESH_INTERVAL_SECONDS,
        jwks_cache: JWKSCache | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        selected = tuple(algorithms)
        rejected = sorted(
            alg for alg in selected if not alg.upper().startswith(_ASYMMETRIC_PREFIXES)
        )
        if rejected:
            raise ValueError(
                f"JWT algorithms must be asymmetric; refusing {rejected}. A JWKS "
                "publishes public keys, so a symmetric secret cannot live there, and "
                "allowing one opens the algorithm-confusion attack."
            )
        if not selected:
            raise ValueError("at least one JWT algorithm must be configured")

        self._issuer = issuer
        self._audience = audience
        self._algorithms = selected
        self._required_scopes = required_scopes
        self._leeway = leeway_seconds
        self._client = client
        self._cache = jwks_cache or JWKSCache(
            jwks_uri,
            allow_private_networks=allow_private_networks,
            min_refresh_interval_seconds=min_refresh_interval_seconds,
        )

    async def authenticate(self, request: Request) -> Principal | None:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None

        token = auth_header[7:].strip()
        if not token:
            return None

        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            logger.debug("rejecting token with unreadable header: %s", exc)
            return None

        key = await self._cache.key_for(unverified_header.get("kid"), client=self._client)
        if key is None:
            logger.info("rejecting token: no signing key for kid %r", unverified_header.get("kid"))
            return None

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            # Deliberately terse: never log the token, only why it was refused.
            logger.info("rejecting token: %s", exc)
            return None

        subject = str(claims.get("sub") or "")
        if not subject:
            logger.info("rejecting token: empty subject")
            return None

        scopes = scopes_from_claims(claims)
        missing = self._required_scopes - scopes
        if missing:
            logger.info("rejecting token for %s: missing scopes %s", subject, sorted(missing))
            return None

        return Principal(
            subject=subject,
            issuer=str(claims.get("iss") or ""),
            scopes=scopes,
            token=token,
            id_token=await self._identity_assertion_for(request, subject),
            expires_at=_expiry_from_claims(claims),
        )

    async def _identity_assertion_for(self, request: Request, subject: str) -> str:
        """Validate an optional caller-supplied ID Token and return it, or "".

        The subject check is the load-bearing part. Without it a caller could
        authenticate with their own access token while attaching someone else's ID
        Token; leg 1 would then mint an ID-JAG for that other person, and the
        downstream server would attribute the call to them. The IdP cannot catch it —
        it only ever sees a validly-signed token for the other subject.

        An unusable assertion is dropped rather than failing the request: the access
        token is still valid, and an EMA server fails closed later (ADR 0006) with an
        error that names the real problem.
        """
        assertion = request.headers.get(IDENTITY_ASSERTION_HEADER, "").strip()
        if not assertion:
            return ""

        try:
            unverified_header = jwt.get_unverified_header(assertion)
        except jwt.PyJWTError as exc:
            logger.info("discarding identity assertion: unreadable header (%s)", exc)
            return ""

        key = await self._cache.key_for(unverified_header.get("kid"), client=self._client)
        if key is None:
            logger.info("discarding identity assertion: no signing key")
            return ""

        try:
            # `aud` is deliberately not verified: an ID Token's audience is the client
            # that requested it, never this hub, so there is nothing here to match.
            assertion_claims = jwt.decode(
                assertion,
                key,
                algorithms=list(self._algorithms),
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "sub"], "verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            logger.info("discarding identity assertion: %s", exc)
            return ""

        if str(assertion_claims.get("sub") or "") != subject:
            logger.warning(
                "discarding identity assertion: subject %r does not match the "
                "authenticated caller %r",
                assertion_claims.get("sub"),
                subject,
            )
            return ""

        return assertion


def _expiry_from_claims(claims: Mapping[str, Any]) -> datetime | None:
    """The `exp` claim as an aware datetime. Already validated by jwt.decode, which
    requires it, so this only has to convert."""
    try:
        return datetime.fromtimestamp(int(claims["exp"]), tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None
