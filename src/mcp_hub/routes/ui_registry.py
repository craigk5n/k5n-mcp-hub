"""Browse the MCP registry and import from it.

Read-only against the registry: the hub imports but never publishes (ADR 0008).
Admin-gated, because the button beside each result registers a server — browsing may
be harmless, but a page whose actions are privileged should not be less protected
than the actions themselves.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from mcp_hub.auth.authorize import require_admin
from mcp_hub.mcp.registry_import import (
    derive_server_id,
    remote_options,
    required_credentials,
    stdio_suggestion,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"])

MAX_RESULTS = 50


async def auth_dependency(request: Request) -> None:
    auth_required_dep = request.app.state.auth_required_dependency
    await auth_required_dep(request)


def _view(record: Any, registry_source: str) -> dict[str, Any]:
    """One result, reduced to what an operator needs to decide.

    Credentials and the stdio caveat are surfaced *before* importing rather than
    discovered afterwards: both change whether the import is the right move at all.
    """
    suggestion = stdio_suggestion(record)
    options = remote_options(record)
    credentials = required_credentials(record.remotes[0]) if record.remotes else []

    return {
        "name": record.name,
        "title": record.title or record.name,
        "description": record.description,
        "version": record.version,
        "repository_url": record.repository_url,
        "server_id": _safe_id(record.name),
        "remotes": [{"url": o.url, "transport": o.transport, "index": o.index} for o in options],
        "credentials": [
            {
                "name": c.name,
                "description": c.description,
                "is_required": c.is_required,
                "is_secret": c.is_secret,
            }
            for c in credentials
        ],
        "stdio_instruction": suggestion.instruction if suggestion else "",
        "stdio_command": suggestion.command_hint if suggestion else "",
        "importable": bool(options),
        "registry_source": registry_source,
    }


def _safe_id(name: str) -> str:
    try:
        return derive_server_id(name)
    except ValueError:
        return ""


@router.get("/registry", response_class=HTMLResponse)
async def registry_page(request: Request, _: None = Depends(auth_dependency)) -> HTMLResponse:
    require_admin(request)
    templates = request.app.state.templates
    settings = getattr(request.app.state, "settings", None)
    template = templates.get_template("registry.html")
    html = await template.render_async(
        registry_source=getattr(getattr(settings, "registry", None), "base_url", ""),
    )
    return HTMLResponse(content=html, media_type="text/html; charset=utf-8")


@router.post("/registry/search", response_class=HTMLResponse)
async def registry_search(
    request: Request,
    q: str = Form(default=""),
    _: None = Depends(auth_dependency),
) -> HTMLResponse:
    require_admin(request)

    templates = request.app.state.templates
    settings = getattr(request.app.state, "settings", None)
    registry_source = getattr(getattr(settings, "registry", None), "base_url", "")

    client = getattr(request.app.state, "registry_client", None)
    error = ""
    results: list[dict[str, Any]] = []

    if client is None:
        error = "No registry client is configured."
    else:
        try:
            records = await client.search(q.strip(), limit=MAX_RESULTS)
            results = [_view(r, registry_source) for r in records]
        except Exception as e:
            # The registry is a third party: it can be down, slow, or rate-limiting.
            # Say so in the panel rather than failing the page.
            logger.warning("registry search failed for %r: %s", q, e)
            error = f"Registry search failed: {e}"

    template = templates.get_template("_registry_results.html")
    html = await template.render_async(
        results=results, query=q, error=error, registry_source=registry_source
    )
    return HTMLResponse(content=html, media_type="text/html; charset=utf-8")
