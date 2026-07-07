from fastapi import APIRouter, Depends, Request
from typing import Callable

from mcp_hub.proxy.handler import proxy_request
from mcp_hub.registry.service import Registry
from mcp_hub.config import TraceConfig

router = APIRouter(prefix="/mcp", tags=["mcp"])


async def auth_dependency(request: Request) -> None:
    auth_required_dep = request.app.state.auth_required_dependency
    await auth_required_dep(request)


async def get_registry(request: Request) -> Registry:
    return request.app.state.registry


async def get_trace_recorder(request: Request) -> object:
    return request.app.state.trace_recorder


async def get_trace_settings(request: Request) -> TraceConfig:
    return request.app.state.settings.trace


async def get_allow_private_networks(request: Request) -> bool:
    return bool(request.app.state.settings.security.allow_private_networks)


def get_proxy_handler() -> Callable:
    return proxy_request


@router.post("")
async def proxy_mcp(
    request: Request,
    handler: Callable = Depends(get_proxy_handler),
    _: None = Depends(auth_dependency),
    registry: Registry = Depends(get_registry),
    trace_recorder: object = Depends(get_trace_recorder),
    settings: TraceConfig = Depends(get_trace_settings),
    allow_private_networks: bool = Depends(get_allow_private_networks),
):
    return await handler(
        request,
        registry,
        trace_recorder,
        settings,
        allow_private_networks=allow_private_networks,
    )


@router.post("/{session_id}")
async def proxy_mcp_with_session(
    request: Request,
    session_id: str,
    handler: Callable = Depends(get_proxy_handler),
    _: None = Depends(auth_dependency),
    registry: Registry = Depends(get_registry),
    trace_recorder: object = Depends(get_trace_recorder),
    settings: TraceConfig = Depends(get_trace_settings),
    allow_private_networks: bool = Depends(get_allow_private_networks),
):
    return await handler(
        request,
        registry,
        trace_recorder,
        settings,
        allow_private_networks=allow_private_networks,
    )


@router.get("/{session_id}")
async def proxy_mcp_get_with_session(
    request: Request,
    session_id: str,
    handler: Callable = Depends(get_proxy_handler),
    _: None = Depends(auth_dependency),
    registry: Registry = Depends(get_registry),
    trace_recorder: object = Depends(get_trace_recorder),
    settings: TraceConfig = Depends(get_trace_settings),
    allow_private_networks: bool = Depends(get_allow_private_networks),
):
    return await handler(
        request,
        registry,
        trace_recorder,
        settings,
        allow_private_networks=allow_private_networks,
    )
