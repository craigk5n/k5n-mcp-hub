import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from mcp_hub.models.server import FaultInjection
from mcp_hub.registry.service import Registry
from mcp_hub.auth.authorize import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"])


async def auth_dependency(request: Request) -> None:
    auth_required_dep = request.app.state.auth_required_dependency
    await auth_required_dep(request)


MAX_FAULT_TIMEOUT_MS = 60000


@router.get("/server/{server_id}/faults", response_class=HTMLResponse)
async def get_server_faults(
    request: Request, server_id: str, _: None = Depends(auth_dependency)
) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    templates = request.app.state.templates

    server = await registry.get(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    fault_injection = server.fault_injection
    if fault_injection is None:
        raise HTTPException(status_code=404, detail="Server not found")

    # Stored timeout_millis of 0 or negative is not meaningful for rendering;
    # default to 2000 ms so the form always shows a usable value.
    timeout_millis = fault_injection.timeout_millis
    if timeout_millis <= 0:
        timeout_millis = 2000

    fault_data = {
        "server_id": server_id,
        "enabled": fault_injection.enabled,
        "timeout_enabled": fault_injection.timeout_enabled,
        "timeout_millis": timeout_millis,
        "malformed_json": fault_injection.malformed_json,
        "invalid_method": fault_injection.invalid_method,
        "sse_interrupt": fault_injection.sse_interrupt,
    }

    template = templates.get_template("faults.html")
    html = await template.render_async(**fault_data)
    return HTMLResponse(content=html)


def _parse_timeout_millis(raw: str | None) -> int:
    if raw is None or raw == "":
        return 2000
    try:
        value = int(raw)
    except ValueError:
        return 2000
    if value <= 0:
        return 2000
    if value > MAX_FAULT_TIMEOUT_MS:
        return MAX_FAULT_TIMEOUT_MS
    return value


@router.post("/server/{server_id}/faults", response_class=HTMLResponse)
async def post_server_faults(
    request: Request, server_id: str, _: None = Depends(auth_dependency)
) -> HTMLResponse:
    registry: Registry = request.app.state.registry
    require_admin(request)
    templates = request.app.state.templates

    server = await registry.get(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    form = await request.form()
    form_data = dict(form)

    enabled = form_data.get("enabled") == "on"
    timeout_enabled = form_data.get("timeout_enabled") == "on"
    malformed_json = form_data.get("malformed_json") == "on"
    invalid_method = form_data.get("invalid_method") == "on"
    sse_interrupt = form_data.get("sse_interrupt") == "on"
    timeout_millis_str = form_data.get("timeout_millis")
    timeout_millis = _parse_timeout_millis(
        timeout_millis_str if isinstance(timeout_millis_str, str) else None
    )

    fault_injection = FaultInjection(
        enabled=enabled,
        timeout_enabled=timeout_enabled,
        timeout_millis=timeout_millis,
        malformed_json=malformed_json,
        invalid_method=invalid_method,
        sse_interrupt=sse_interrupt,
    )

    server.fault_injection = fault_injection
    await registry.register(server)

    fault_data = {
        "server_id": server_id,
        "enabled": enabled,
        "timeout_enabled": timeout_enabled,
        "timeout_millis": timeout_millis,
        "malformed_json": malformed_json,
        "invalid_method": invalid_method,
        "sse_interrupt": sse_interrupt,
    }

    template = templates.get_template("faults.html")
    html = await template.render_async(**fault_data)
    return HTMLResponse(content=html)
