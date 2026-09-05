from __future__ import annotations

from fastapi import Request

from mcp_hub.auth.principal import Principal


class NoAuthStrategy:
    async def authenticate(self, request: Request) -> Principal | None:
        return Principal.anonymous()
