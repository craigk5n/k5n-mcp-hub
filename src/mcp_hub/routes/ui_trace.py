import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from mcp_hub.registry.service import Registry
from mcp_hub.trace.recorder import TraceRecorder
from mcp_hub.auth.authorize import request_is_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"])


async def auth_dependency(request: Request) -> None:
    auth_required_dep = request.app.state.auth_required_dependency
    await auth_required_dep(request)


@router.get("/server/{server_id:path}/trace", response_class=HTMLResponse)
async def get_trace(
    request: Request, server_id: str, _: None = Depends(auth_dependency)
) -> HTMLResponse:
    if not server_id:
        raise HTTPException(status_code=400, detail="Server ID is required")

    registry: Registry = request.app.state.registry
    trace_recorder: TraceRecorder = request.app.state.trace_recorder
    templates = request.app.state.templates

    srv = await registry.get(server_id)
    verbose = srv.trace_verbose if srv else False

    entries = trace_recorder.list(server_id, subject=_trace_subject(request))

    template = templates.get_template("trace.html")
    html = await template.render_async(
        server_id=server_id,
        entries=entries,
        verbose=verbose,
    )
    return HTMLResponse(content=html)


@router.post("/server/{server_id:path}/trace/verbose", response_class=HTMLResponse)
async def toggle_verbose_trace(
    request: Request, server_id: str, _: None = Depends(auth_dependency)
) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    trace_recorder: TraceRecorder = request.app.state.trace_recorder
    templates = request.app.state.templates
    registry_lock: asyncio.Lock = request.app.state.registry_lock

    async with registry_lock:
        srv = await registry.get(server_id)
        if srv is None:
            raise HTTPException(status_code=404, detail="Server not found")

        srv.trace_verbose = not srv.trace_verbose
        try:
            await registry.register(srv)
        except Exception as e:
            logger.error(f"Failed to persist server {server_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed to update server configuration")

    try:
        entries = trace_recorder.list(server_id, subject=_trace_subject(request))
    except Exception as e:
        logger.error(f"Failed to list trace entries for {server_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve trace entries")

    template = templates.get_template("trace.html")
    html = await template.render_async(
        server_id=server_id,
        entries=entries,
        verbose=srv.trace_verbose,
    )
    return HTMLResponse(content=html)


@router.post("/server/{server_id:path}/trace/clear", response_class=HTMLResponse)
async def clear_trace(
    request: Request, server_id: str, _: None = Depends(auth_dependency)
) -> HTMLResponse:
    if not server_id:
        raise HTTPException(status_code=400, detail="Server ID is required")

    registry: Registry = request.app.state.registry
    trace_recorder: TraceRecorder = request.app.state.trace_recorder
    templates = request.app.state.templates

    trace_recorder.clear(server_id)

    srv = await registry.get(server_id)
    verbose = srv.trace_verbose if srv else False

    entries = trace_recorder.list(server_id, subject=_trace_subject(request))

    template = templates.get_template("trace.html")
    html = await template.render_async(
        server_id=server_id,
        entries=entries,
        verbose=verbose,
    )
    return HTMLResponse(content=html)


def _trace_subject(request: Request) -> str | None:
    """Which caller's entries to show: None means all.

    Admins and the single-user auth modes see everything; anyone else sees only the
    requests they made, so a shared server's trace cannot disclose another caller's
    arguments and timings."""
    from mcp_hub.auth.caller import caller_from_request
    from mcp_hub.auth.principal import Principal

    if request_is_admin(request):
        return None
    caller = caller_from_request(request)
    return caller.subject if isinstance(caller, Principal) else ""
