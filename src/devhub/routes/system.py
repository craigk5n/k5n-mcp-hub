from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse

from devhub.metrics import metrics

BASE_DIR = Path(__file__).parent.parent

router = APIRouter(tags=["system"])


@router.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    index_path = BASE_DIR / "static" / "index.html"
    content = index_path.read_text(encoding="utf-8")
    return HTMLResponse(content=content, media_type="text/html; charset=utf-8")


@router.get("/healthz")
async def healthz_check() -> PlainTextResponse:
    return PlainTextResponse(content="ok", media_type="text/plain; charset=utf-8")


@router.get("/metrics")
async def metrics_endpoint() -> PlainTextResponse:
    return PlainTextResponse(
        content=metrics.render_prometheus(),
        media_type="text/plain; charset=utf-8",
    )
