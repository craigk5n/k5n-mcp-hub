from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import httpx
import jwt

from mcp_hub.utils import safe_http_client_factory

logger = logging.getLogger(__name__)

# Every URN the Enterprise-Managed Authorization extension pins, in one place. ID-JAG
# is an active IETF draft rather than a finished RFC (ADR 0005), so a wire-format
# revision should be an edit here and nowhere else.
TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
JWT_BEARER_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
ID_JAG_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id-jag"
ID_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
ID_JAG_GRANT_PROFILE = "urn:ietf:params:oauth:grant-profile:id-jag"

# The MCP extension a client declares when it speaks this flow.
EMA_EXTENSION = "io.modelcontextprotocol/enterprise-managed-authorization"

ID_JAG_TIMEOUT_SECONDS = 10.0
MIN_TOKEN_LIFETIME_SECONDS = 30
DEFAULT_EXPIRES_IN = 300


class IDJagError(Exception):
    """One of the two legs failed.

    ``leg`` matters: "the enterprise IdP refused" (policy, wrong audience) and "the
    backend's authorization server refused" (assertion rejected, unknown client) have
    completely different fixes, and collapsing them wastes the operator's time.
    Carries no token material.
    """

    def __init__(
        self,
        message: str,
        *,
        leg: int,
        error: str = "",
        error_description: str = "",
        status_code: int = 0,
    ) -> None:
        super().__init__(message)
        self.leg = leg
        self.error = error
        self.error_description = error_description
        self.status_code = status_code

    def summary(self) -> str:
        where = "leg 1 (enterprise IdP)" if self.leg == 1 else "leg 2 (resource AS)"
        detail = (
            f"{self.error}: {self.error_description}"
            if self.error and self.error_description
            else self.error or str(self)
        )
        return f"{where}: {detail}"


@dataclass(frozen=True)
class IDJagRequest:
    idp_token_url: str
    resource_as_token_url: str
    # MUST be the *issuer identifier* of the resource authorization server — not the
    # MCP server's URL, which is what `resource_id` carries.
    resource_as_issuer: str
    resource_id: str
    client_id: str
    client_secret: str
    subject_token: str
    subject_token_type: str = ID_TOKEN_TYPE
    scope: str = ""


@dataclass(frozen=True)
class IDJagAccessToken:
    access_token: str
    expires_in: int = DEFAULT_EXPIRES_IN
    scope: str = ""

    def __repr__(self) -> str:
        return (
            f"IDJagAccessToken(access_token='<redacted>', "
            f"expires_in={self.expires_in!r}, scope={self.scope!r})"
        )


def supports_id_jag_profile(metadata: Mapping[str, Any] | None) -> bool:
    """Whether an authorization server advertises the ID-JAG grant profile.

    Advisory only: an AS may accept the grant without advertising it, so callers must
    not treat a False here as a reason to refuse to try."""
    if not metadata:
        return False
    profiles = metadata.get("authorization_grant_profiles_supported")
    if not isinstance(profiles, list):
        return False
    return ID_JAG_GRANT_PROFILE in profiles


def build_id_jag_client(*, allow_private_networks: bool = False) -> httpx.AsyncClient:
    """An identity assertion travels on leg 1 and the ID-JAG on leg 2, so both must be
    pinned to a validated IP and must never follow a redirect."""
    return safe_http_client_factory(
        timeout=httpx.Timeout(ID_JAG_TIMEOUT_SECONDS),
        allow_private_networks=allow_private_networks,
    )


async def exchange_for_access_token(
    request: IDJagRequest,
    *,
    client: httpx.AsyncClient | None = None,
    allow_private_networks: bool = False,
) -> IDJagAccessToken:
    """Run both legs and return the downstream access token."""
    own_client = client is None
    http_client = (
        client
        if client is not None
        else build_id_jag_client(allow_private_networks=allow_private_networks)
    )
    try:
        assertion = await _request_id_jag(http_client, request)
        _verify_resource_claim(assertion, request.resource_id)
        return await _redeem_id_jag(http_client, request, assertion)
    finally:
        if own_client:
            await http_client.aclose()


async def _request_id_jag(client: httpx.AsyncClient, request: IDJagRequest) -> str:
    form = {
        "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
        "subject_token": request.subject_token,
        "subject_token_type": request.subject_token_type,
        "requested_token_type": ID_JAG_TOKEN_TYPE,
        "audience": request.resource_as_issuer,
        "client_id": request.client_id,
        "client_secret": request.client_secret,
    }
    if request.resource_id:
        form["resource"] = request.resource_id
    if request.scope:
        form["scope"] = request.scope

    payload = await _post(client, request.idp_token_url, form, leg=1)

    issued_type = str(payload.get("issued_token_type") or "")
    if issued_type != ID_JAG_TOKEN_TYPE:
        # Forwarding whatever came back would defeat the point of asking for an ID-JAG.
        raise IDJagError(
            f"expected issued_token_type {ID_JAG_TOKEN_TYPE}, got {issued_type!r}",
            leg=1,
        )

    assertion = payload.get("access_token")
    if not assertion or not isinstance(assertion, str):
        raise IDJagError("the IdP returned no assertion", leg=1)
    return assertion


def _verify_resource_claim(assertion: str, expected_resource: str) -> None:
    """The spec makes `resource` MUST-contain the MCP server's identifier. Checking it
    before leg 2 stops an assertion minted for a different server being redeemed
    against this one — a confused deputy the resource AS might well accept."""
    if not expected_resource:
        return
    try:
        claims = jwt.decode(assertion, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise IDJagError(f"the assertion is not a readable JWT: {exc}", leg=1) from exc

    actual = str(claims.get("resource") or "")
    if actual != expected_resource:
        raise IDJagError(
            f"assertion resource claim {actual!r} does not name {expected_resource!r}",
            leg=1,
        )


async def _redeem_id_jag(
    client: httpx.AsyncClient, request: IDJagRequest, assertion: str
) -> IDJagAccessToken:
    form = {
        "grant_type": JWT_BEARER_GRANT_TYPE,
        "assertion": assertion,
        "client_id": request.client_id,
        "client_secret": request.client_secret,
    }
    if request.scope:
        form["scope"] = request.scope

    payload = await _post(client, request.resource_as_token_url, form, leg=2)

    access_token = payload.get("access_token")
    if not access_token or not isinstance(access_token, str):
        raise IDJagError("the resource AS returned no access_token", leg=2)

    return IDJagAccessToken(
        access_token=access_token,
        expires_in=_coerce_expires_in(payload.get("expires_in")),
        scope=str(payload.get("scope") or ""),
    )


async def _post(
    client: httpx.AsyncClient, url: str, form: dict[str, str], *, leg: int
) -> dict[str, Any]:
    try:
        response = await client.post(
            url,
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
    except httpx.HTTPError as exc:
        raise IDJagError(f"token endpoint unreachable: {exc}", leg=leg) from exc

    if response.status_code >= 400:
        raise _error_from_response(response, leg=leg)

    try:
        payload = response.json()
    except ValueError as exc:
        raise IDJagError("token endpoint returned a non-JSON body", leg=leg) from exc
    if not isinstance(payload, dict):
        raise IDJagError("token endpoint returned an unexpected body", leg=leg)
    return payload


def _error_from_response(response: httpx.Response, *, leg: int) -> IDJagError:
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
    return IDJagError(
        f"leg {leg} failed ({response.status_code}){': ' + detail if detail else ''}",
        leg=leg,
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
