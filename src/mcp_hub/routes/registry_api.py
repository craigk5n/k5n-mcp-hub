import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from mcp_hub.models import RegisteredServer
from mcp_hub.registry.service import Registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/register", tags=["registry"])


@dataclass
class ServersResponse:
    servers: list[RegisteredServer]


def get_registry(request: Request) -> Registry:
    return request.app.state.registry


async def auth_dependency(request: Request) -> None:
    auth_required_dep = request.app.state.auth_required_dependency
    await auth_required_dep(request)


@router.delete("/{id}", status_code=204)
async def unregister_server(
    id: str,
    request: Request,
    registry: Registry = Depends(get_registry),
    _: None = Depends(auth_dependency),
) -> PlainTextResponse:
    stripped_id = id.strip()
    if not stripped_id:
        return PlainTextResponse("id is required", status_code=400)

    try:
        await registry.unregister(stripped_id)
    except KeyError as e:
        logger.error("Failed to unregister server %s: %s", stripped_id, e)
        return PlainTextResponse(
            f"unregister failed: {e}",
            status_code=500,
        )
    except Exception as e:
        logger.exception("Unexpected error unregistering server %s", stripped_id)
        return PlainTextResponse(
            f"unregister failed: {e}",
            status_code=500,
        )

    return PlainTextResponse("", status_code=204)


async def list_servers_handler(
    registry: Registry = Depends(get_registry),
) -> ServersResponse:
    servers = await registry.list()
    return ServersResponse(servers=[s.sanitize_for_api() for s in servers])


async def get_server_handler(
    id: str,
    registry: Registry = Depends(get_registry),
) -> RegisteredServer | PlainTextResponse:
    stripped_id = id.strip()
    if not stripped_id:
        return PlainTextResponse("id is required", status_code=400)

    try:
        server = await registry.get(stripped_id)
    except Exception as e:
        logger.exception("Unexpected error fetching server %s", stripped_id)
        return PlainTextResponse(
            f"get failed: {e}",
            status_code=500,
        )

    if server is None:
        return PlainTextResponse("not found", status_code=404)

    return server.sanitize_for_api()


v1_servers_router = APIRouter(prefix="/v1", tags=["v1-servers"])
v1_servers_router.add_api_route(
    "/servers",
    list_servers_handler,
    methods=["GET"],
    response_model=ServersResponse,
)
v1_servers_router.add_api_route(
    "/servers/{id}",
    get_server_handler,
    methods=["GET"],
    response_model=None,
)


api_servers_router = APIRouter(prefix="/api", tags=["api-servers"])
api_servers_router.add_api_route(
    "/servers",
    list_servers_handler,
    methods=["GET"],
    response_model=ServersResponse,
)
