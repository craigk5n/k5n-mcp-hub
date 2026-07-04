import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from devhub.registry.service import Registry
from devhub.trace.recorder import TraceRecorder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"])


@router.get("/server/{server_id:path}/trace", response_class=HTMLResponse)
async def get_trace(request: Request, server_id: str) -> HTMLResponse:
    if not server_id:
        raise HTTPException(status_code=400, detail="Server ID is required")

    registry: Registry = request.app.state.registry
    trace_recorder: TraceRecorder = request.app.state.trace_recorder
    templates = request.app.state.templates

    srv = await registry.get(server_id)
    verbose = srv.trace_verbose if srv else False

    entries = trace_recorder.list(server_id)

    template = templates.get_template("trace.html")
    html = await template.render_async(
        server_id=server_id,
        entries=entries,
        verbose=verbose,
    )
    return HTMLResponse(content=html)


@router.post("/server/{server_id:path}/trace/verbose", response_class=HTMLResponse)
async def toggle_verbose_trace(request: Request, server_id: str) -> HTMLResponse:
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
        entries = trace_recorder.list(server_id)
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
async def clear_trace(request: Request, server_id: str) -> HTMLResponse:
    if not server_id:
        raise HTTPException(status_code=400, detail="Server ID is required")

    registry: Registry = request.app.state.registry
    trace_recorder: TraceRecorder = request.app.state.trace_recorder
    templates = request.app.state.templates

    trace_recorder.clear(server_id)

    srv = await registry.get(server_id)
    verbose = srv.trace_verbose if srv else False

    entries = trace_recorder.list(server_id)

    template = templates.get_template("trace.html")
    html = await template.render_async(
        server_id=server_id,
        entries=entries,
        verbose=verbose,
    )
    return HTMLResponse(content=html)
