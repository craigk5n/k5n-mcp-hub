from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from mcp_hub.utils import safe_http_client_factory

logger = logging.getLogger(__name__)

TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

EXCHANGE_TIMEOUT_SECONDS = 10.0
# Floor for a cached token's lifetime, matching the existing client-credentials cache.
MIN_TOKEN_LIFETIME_SECONDS = 30
DEFAULT_EXPIRES_IN = 300


class TokenExchangeError(Exception):
    """A token exchange failed.

    Carries the IdP's own RFC 6749 ``error``/``error_description`` because those name
    the actual misconfiguration (``invalid_target`` = the audience isn't registered);
    collapsing them into "exchange failed" throws away the only useful diagnostic.
    Never carries token material.
    """

    def __init__(
        self,
        message: str,
        *,
        error: str = "",
        error_description: str = "",
        status_code: int = 0,
    ) -> None:
        super().__init__(message)
        self.error = error
        self.error_description = error_description
        self.status_code = status_code

    def summary(self) -> str:
        if self.error and self.error_description:
            return f"{self.error}: {self.error_description}"
        return self.error or str(self)


@dataclass(frozen=True)
class ExchangeRequest:
    token_url: str
    client_id: str
    client_secret: str
    subject_token: str
    audience: str = ""
    resource: str = ""
    scope: str = ""
    # Delegation is opt-in (ADR 0002): sent only when the server is configured for an
    # issuer that implements RFC 8693's actor token.
    actor_token: str = ""
    requested_token_type: str = ACCESS_TOKEN_TYPE

    def to_form(self) -> dict[str, str]:
        form = {
            "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
            "subject_token": self.subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if self.requested_token_type:
            form["requested_token_type"] = self.requested_token_type
        if self.audience:
            form["audience"] = self.audience
        if self.resource:
            form["resource"] = self.resource
        if self.scope:
            form["scope"] = self.scope
        if self.actor_token:
            form["actor_token"] = self.actor_token
            form["actor_token_type"] = ACCESS_TOKEN_TYPE
        return form


@dataclass(frozen=True)
class ExchangedToken:
    access_token: str
    expires_in: int = DEFAULT_EXPIRES_IN
    issued_token_type: str = ""
    scope: str = ""

    def __repr__(self) -> str:
        return (
            f"ExchangedToken(access_token='<redacted>', expires_in={self.expires_in!r}, "
            f"issued_token_type={self.issued_token_type!r}, scope={self.scope!r})"
        )


def build_exchange_client(*, allow_private_networks: bool = False) -> httpx.AsyncClient:
    """The subject token leaves the process on this request, so it must be pinned to a
    validated IP and must never follow a redirect — a 3xx to an internal URL would leak
    both the subject token and the hub's client secret."""
    return safe_http_client_factory(
        timeout=httpx.Timeout(EXCHANGE_TIMEOUT_SECONDS),
        allow_private_networks=allow_private_networks,
    )


async def exchange_token(
    request: ExchangeRequest,
    *,
    client: httpx.AsyncClient | None = None,
    allow_private_networks: bool = False,
) -> ExchangedToken:
    own_client = client is None
    http_client = (
        client
        if client is not None
        else build_exchange_client(allow_private_networks=allow_private_networks)
    )

    try:
        response = await http_client.post(
            request.token_url,
            data=request.to_form(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
    except httpx.HTTPError as exc:
        raise TokenExchangeError(f"token endpoint unreachable: {exc}") from exc
    finally:
        if own_client:
            await http_client.aclose()

    if response.status_code >= 400:
        raise _error_from_response(response)

    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise TokenExchangeError("token endpoint returned a non-JSON body") from exc

    if not isinstance(payload, dict):
        raise TokenExchangeError("token endpoint returned an unexpected body")

    access_token = payload.get("access_token")
    if not access_token or not isinstance(access_token, str):
        raise TokenExchangeError("token response contained no access_token")

    return ExchangedToken(
        access_token=access_token,
        expires_in=_coerce_expires_in(payload.get("expires_in")),
        issued_token_type=str(payload.get("issued_token_type") or ""),
        scope=str(payload.get("scope") or ""),
    )


def _error_from_response(response: httpx.Response) -> TokenExchangeError:
    error = ""
    description = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = str(payload.get("error") or "")
            description = str(payload.get("error_description") or "")
    except ValueError:
        pass

    detail = f"{error}: {description}" if error and description else error
    return TokenExchangeError(
        f"token exchange failed ({response.status_code}){': ' + detail if detail else ''}",
        error=error,
        error_description=description,
        status_code=response.status_code,
    )


def _coerce_expires_in(value: object) -> int:
    try:
        seconds = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return DEFAULT_EXPIRES_IN
    return max(seconds, MIN_TOKEN_LIFETIME_SECONDS)
