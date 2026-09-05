from __future__ import annotations

from typing import Awaitable, Callable, Protocol, runtime_checkable

from fastapi import HTTPException, Request

from mcp_hub.auth.principal import Principal


@runtime_checkable
class Authenticator(Protocol):
    async def authenticate(self, request: Request) -> Principal | None: ...


AuthRequiredDependency = Callable[[Request], Awaitable[None]]


def auth_required(
    authenticator: Authenticator,
    www_authenticate_header: str = 'Basic realm="Restricted"',
    challenge_factory: Callable[[Request], str] | None = None,
) -> AuthRequiredDependency:
    """Build the 401-on-failure dependency.

    ``challenge_factory`` lets the challenge depend on the request, which RFC 9728
    needs: the metadata URL it advertises is built from the hub's own base URL and
    isn't known when the app is constructed."""

    async def dependency(request: Request) -> None:
        principal = await authenticator.authenticate(request)
        # An explicit isinstance check rather than a truthiness test: a strategy still
        # on the old boolean contract must fail closed, not be read as "authenticated
        # with an unknown identity".
        if not isinstance(principal, Principal):
            challenge = (
                challenge_factory(request)
                if challenge_factory is not None
                else www_authenticate_header
            )
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": challenge},
            )
        # Routes keep declaring `_: None = Depends(auth_dependency)`; the identity
        # travels on the request instead of through the dependency's return value, so
        # no existing route signature changes.
        request.state.principal = principal
        return None

    return dependency
