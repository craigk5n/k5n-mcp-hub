from __future__ import annotations

from typing import Awaitable, Callable, Protocol, runtime_checkable

from fastapi import HTTPException, Request


@runtime_checkable
class Authenticator(Protocol):
    async def authenticate(self, request: Request) -> bool: ...


AuthRequiredDependency = Callable[[Request], Awaitable[None]]


def auth_required(
    authenticator: Authenticator,
    www_authenticate_header: str = 'Basic realm="Restricted"',
) -> AuthRequiredDependency:
    async def dependency(request: Request) -> None:
        result = await authenticator.authenticate(request)
        if result is not True:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": www_authenticate_header},
            )
        return None

    return dependency
