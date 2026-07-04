from fastapi import APIRouter, Depends, Request

from devhub.models import RegisteredServer
from devhub.registry.service import Registry

router = APIRouter(prefix="/api", tags=["api"])


def get_registry(request: Request) -> Registry:
    return request.app.state.registry


@router.get("/servers", response_model=list[RegisteredServer])
async def list_servers(
    registry: Registry = Depends(get_registry),
) -> list[RegisteredServer]:
    servers = await registry.list()
    return [s.sanitize_for_api() for s in servers]
