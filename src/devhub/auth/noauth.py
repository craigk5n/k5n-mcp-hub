from __future__ import annotations

from fastapi import Request


class NoAuthStrategy:
    async def authenticate(self, request: Request) -> bool:
        return True
