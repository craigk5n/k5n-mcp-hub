from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from mcp_hub.auth.metadata import (
    PROTECTED_RESOURCE_METADATA_PATH,
    protected_resource_metadata,
)
from mcp_hub.metrics import metrics

BASE_DIR = Path(__file__).parent.parent

router = APIRouter(tags=["system"])


@router.get(PROTECTED_RESOURCE_METADATA_PATH)
async def protected_resource_metadata_endpoint(request: Request) -> JSONResponse:
    """RFC 9728 metadata. Served only when the hub is actually a resource server —
    advertising an authorization server the hub does not enforce would be a lie a
    client acts on."""
    settings = request.app.state.settings
    if settings.auth.type != "jwt":
        raise HTTPException(status_code=404, detail="Not Found")
    return JSONResponse(protected_resource_metadata(request, settings.auth))


@router.get("/")
async def root() -> RedirectResponse:
    # Send users to the fully-wired server page. The old landing page nested the whole
    # /ui/servers document inside a <div> via hx-swap, which broke htmx processing of the
    # card buttons; /ui/servers now carries the Add Server form itself, so it's the single
    # coherent page.
    return RedirectResponse(url="/ui/servers", status_code=307)


@router.get("/healthz")
async def healthz_check() -> PlainTextResponse:
    return PlainTextResponse(content="ok", media_type="text/plain; charset=utf-8")


@router.get("/metrics")
async def metrics_endpoint() -> PlainTextResponse:
    return PlainTextResponse(
        content=metrics.render_prometheus(),
        media_type="text/plain; charset=utf-8",
    )
