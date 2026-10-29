"""Admin pages for OpenAPI servers (Story 13.8).

- `/ui/openapi`: the OpenAPI servers, and the import form (URL, paste or file) with
  job progress.
- `/ui/openapi/{id}`: the settings (base URL confirmed from the spec's `servers[]`,
  credentials with the spec's security schemes as suggestions), the operation
  picker, the health probe, and "check for updates".
- `/ui/openapi/{id}/revision`: the diff of a pending revision, with accept and reject.

The pages render; the forms post to the `/v1/openapi` JSON API, which does every
check. All spec text reaches the page through Jinja's autoescaping: a spec is
untrusted, and its descriptions are exactly where markup would be planted.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from mcp_hub.auth.authorize import require_admin
from mcp_hub.models.server import RegisteredServer
from mcp_hub.openapi.hints import operation_tags, security_scheme_hints, suggested_base_urls
from mcp_hub.openapi.model import MappedOperation, OperationKey
from mcp_hub.openapi.revision import diff_revision
from mcp_hub.openapi.server_tools import openapi_preview

router = APIRouter(prefix="/ui", tags=["ui-openapi"])

_UNTAGGED = "Untagged"


async def _admin(request: Request) -> None:
    await request.app.state.auth_required_dependency(request)
    require_admin(request)


async def _render(request: Request, template: str, **context: Any) -> HTMLResponse:
    html = await request.app.state.templates.get_template(template).render_async(**context)
    return HTMLResponse(content=html)


async def _server(request: Request, server_id: str) -> RegisteredServer:
    server = await request.app.state.registry.get(server_id)
    if server is None or not server.is_openapi:
        raise HTTPException(status_code=404, detail="no such OpenAPI server")
    return server  # type: ignore[no-any-return]


def _summary(server: RegisteredServer) -> dict[str, Any]:
    """What the pages show about a server: never its secrets or its spec document."""
    snapshot, pending = server.openapi_snapshot, server.openapi_pending
    return {
        "id": server.id,
        "name": server.name,
        "description": server.description,
        "base_url": server.openapi_base_url,
        "required_scope": server.required_scope,
        "auth_type": server.auth_type,
        "api_key_name": server.api_key_name,
        "api_key_in": server.api_key_in,
        "basic_username": server.basic_username,
        "oauth_token_url": server.oauth_token_url,
        "oauth_client_id": server.oauth_client_id,
        "obo_audience": server.obo_audience,
        "has_secret": bool(
            server.bearer_token
            or server.api_key_value
            or server.basic_password
            or server.oauth_client_secret
        ),
        "title": str(snapshot.document.get("info", {}).get("title", "")) if snapshot else "",
        "dialect": snapshot.dialect if snapshot else "",
        "sha256": snapshot.sha256 if snapshot else "",
        "source_url": next((s.source_url for s in (pending, snapshot) if s and s.source_url), ""),
        "pending_sha256": pending.sha256 if pending else "",
        "selected": len(server.openapi_selection),
        "healthy": server.healthy,
    }


@router.get("/openapi", response_class=HTMLResponse)
async def openapi_index(request: Request) -> HTMLResponse:
    await _admin(request)
    servers = [s for s in await request.app.state.registry.list() if s.is_openapi]
    return await _render(request, "openapi.html", servers=[_summary(s) for s in servers])


@router.get("/openapi/jobs/{job_id}", response_class=HTMLResponse)
async def openapi_job(request: Request, job_id: str) -> HTMLResponse:
    await _admin(request)
    job = request.app.state.openapi_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such import job")
    return await _render(request, "_openapi_job.html", job=job.to_json())


def _operation_view(mapped: MappedOperation, server: RegisteredServer) -> dict[str, Any]:
    override = next((o for o in server.openapi_overrides if o.key() == mapped.key), None)
    tool = mapped.tool
    schema = tool.input_schema if tool else {}
    required = set(schema.get("required", []))
    return {
        "method": mapped.key.method,
        "path": mapped.key.path,
        "selected": mapped.selected,
        "supported": tool is not None,
        "tool_name": tool.name if tool else "",
        "description": tool.description if tool else "",
        "arguments": [
            {"name": name, "required": name in required} for name in schema.get("properties", {})
        ],
        "unsupported": list(mapped.unsupported),
        "dropped": list(mapped.dropped),
        "problems": list(mapped.problems),
        "notes": list(mapped.notes),
        "override_name": (override.name or "") if override else "",
        "override_description": (override.description or "") if override else "",
        # A probe runs on its own: a supported GET with no required inputs.
        "probe_eligible": mapped.key.method == "GET" and tool is not None and not required,
    }


def _grouped(
    operations: tuple[MappedOperation, ...],
    tags: dict[OperationKey, list[str]],
    server: RegisteredServer,
) -> list[dict[str, Any]]:
    """Operations by their first tag (each listed once), in spec order."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for mapped in operations:
        tag = (tags.get(mapped.key) or [_UNTAGGED])[0]
        groups.setdefault(tag, []).append(_operation_view(mapped, server))
    return [{"tag": tag, "operations": views} for tag, views in groups.items()]


@router.get("/openapi/{server_id}", response_class=HTMLResponse)
async def openapi_server(request: Request, server_id: str) -> HTMLResponse:
    await _admin(request)
    server = await _server(request, server_id)
    limit = request.app.state.settings.openapi.max_description_chars
    operations = await asyncio.to_thread(openapi_preview, server, description_limit=limit)
    snapshot = server.openapi_snapshot
    tags = operation_tags(snapshot) if snapshot else {}
    groups = _grouped(operations, tags, server)
    probe = server.openapi_health_probe
    return await _render(
        request,
        "openapi_server.html",
        server=_summary(server),
        suggestions=suggested_base_urls(snapshot) if snapshot else [],
        security_hints=security_scheme_hints(snapshot) if snapshot else [],
        groups=groups,
        probe_options=[
            f"{op['method']} {op['path']}"
            for group in groups
            for op in group["operations"]
            if op["probe_eligible"]
        ],
        current_probe=f"{probe.method} {probe.path}" if probe else "",
    )


@router.get("/openapi/{server_id}/revision", response_class=HTMLResponse)
async def openapi_revision(request: Request, server_id: str) -> HTMLResponse:
    await _admin(request)
    server = await _server(request, server_id)
    if server.openapi_pending is None:
        raise HTTPException(status_code=404, detail="no pending revision")
    limit = request.app.state.settings.openapi.max_description_chars
    diff = await asyncio.to_thread(diff_revision, server, description_limit=limit)
    return await _render(
        request, "openapi_revision.html", server=_summary(server), diff=diff.to_json()
    )
