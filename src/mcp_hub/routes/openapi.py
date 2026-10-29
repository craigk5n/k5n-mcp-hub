"""`/v1/openapi`: import REST APIs as MCP servers and choose their tools (Story 13.4).

All admin-only (`require_admin`: the admin scope under `jwt`, everyone otherwise, as
for `/v1/register`). Importing is a background job because loading a spec can take
minutes; everything that can be checked up front -- the request's shape, the base
URL and spec URL against the SSRF guard, the size, the id -- is checked before the
job starts, so the common mistakes come back at once.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from mcp_hub.auth.authorize import require_admin
from mcp_hub.models.openapi import OpenAPIOperationRef, OpenAPIOverride
from mcp_hub.models.server import RegisteredServer
from mcp_hub.openapi.importer import OpenAPIImportRequest, apply_choices, run_import
from mcp_hub.openapi.jobs import ImportJobStore, JobQueueFull
from mcp_hub.openapi.model import MappedOperation, MappingError
from mcp_hub.openapi.server_tools import openapi_preview
from mcp_hub.utils import is_url_safe_for_discovery

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/openapi", tags=["openapi"])

# Room for the JSON envelope and credential fields around an inline spec.
_ENVELOPE_BYTES = 64 * 1024
# A selection plus overrides for every operation of a large spec fits well inside.
_TOOLS_BODY_BYTES = 1024 * 1024


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """The body, or None once it is known to exceed `limit` -- from Content-Length
    when declared, otherwise while streaming, so an oversized body is never buffered."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


async def auth_dependency(request: Request) -> None:
    await request.app.state.auth_required_dependency(request)


def _jobs(request: Request) -> ImportJobStore:
    return request.app.state.openapi_jobs  # type: ignore[no-any-return]


def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, **extra})


@router.post("/imports", status_code=202, response_model=None)
async def start_import(request: Request, _: None = Depends(auth_dependency)) -> JSONResponse:
    require_admin(request)
    settings = request.app.state.settings
    body = await _read_capped(request, settings.openapi.max_spec_bytes + _ENVELOPE_BYTES)
    if body is None:
        return _error(413, "request too large (openapi.max_spec_bytes)")
    try:
        validated = OpenAPIImportRequest.model_validate(json.loads(body))
    except json.JSONDecodeError:
        return _error(400, "invalid json")
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ()))
        return _error(400, f"{where + ': ' if where else ''}{first.get('msg', 'invalid value')}")
    if len(validated.spec.encode("utf-8")) > settings.openapi.max_spec_bytes:
        return _error(413, "spec too large (openapi.max_spec_bytes)")

    existing = await request.app.state.registry.get(validated.id)
    if existing is not None and not existing.is_openapi:
        return _error(409, f"{validated.id!r} is already registered as a non-OpenAPI server")
    if existing is None and not validated.base_url:
        return _error(400, "base_url is required for a new OpenAPI server")

    allow_private = settings.security.allow_private_networks
    for url, require_reachability in ((validated.base_url, True), (validated.spec_url, False)):
        if url:
            safe, _reason, _ips = await is_url_safe_for_discovery(
                url, require_reachability, allow_private
            )
            if not safe:
                # The same reply /v1/register gives, so it says no more about why.
                return _error(400, "URL validation failed")

    async def work() -> str:
        return await run_import(
            validated,
            registry=request.app.state.registry,
            discovery_service=request.app.state.discovery_service,
            settings=settings,
            locks=request.app.state.openapi_locks,
        )

    try:
        job = _jobs(request).submit(validated.id, work)
    except JobQueueFull as exc:
        return _error(429, str(exc))
    location = f"/v1/openapi/imports/{job.id}"
    return JSONResponse(
        status_code=202,
        content={**job.to_json(), "status_url": location},
        headers={"Location": location},
    )


@router.get("/imports/{job_id}", response_model=None)
async def get_import(
    job_id: str, request: Request, _: None = Depends(auth_dependency)
) -> JSONResponse:
    require_admin(request)
    job = _jobs(request).get(job_id)
    if job is None:
        return _error(404, "no such import job (finished jobs are kept for an hour)")
    return JSONResponse(content=job.to_json())


async def _openapi_server(request: Request, server_id: str) -> RegisteredServer | None:
    server = await request.app.state.registry.get(server_id)
    return server if server is not None and server.is_openapi else None


@router.get("/servers/{server_id}/operations", response_model=None)
async def get_operations(
    server_id: str, request: Request, _: None = Depends(auth_dependency)
) -> JSONResponse:
    require_admin(request)
    server = await _openapi_server(request, server_id)
    if server is None:
        return _error(404, "no such OpenAPI server")
    limit = request.app.state.settings.openapi.max_description_chars
    pending = server.openapi_pending
    probe = server.openapi_health_probe
    # Mapping a large spec takes most of a second: off the event loop.
    operations = await asyncio.to_thread(openapi_preview, server, description_limit=limit)
    return JSONResponse(
        content={
            "server_id": server.id,
            "base_url": server.openapi_base_url,
            "sha256": server.openapi_snapshot.sha256 if server.openapi_snapshot else None,
            "pending": (
                {
                    "sha256": pending.sha256,
                    "loaded_at": pending.loaded_at.isoformat() if pending.loaded_at else None,
                }
                if pending
                else None
            ),
            "health_probe": probe.model_dump() if probe else None,
            "operations": [_operation_json(m) for m in operations],
        }
    )


def _operation_json(mapped: MappedOperation) -> dict[str, Any]:
    return {
        "method": mapped.key.method,
        "path": mapped.key.path,
        "selected": mapped.selected,
        "tool": mapped.tool.to_mcp() if mapped.tool else None,
        "unsupported": list(mapped.unsupported),
        "dropped": list(mapped.dropped),
        "problems": list(mapped.problems),
        "notes": list(mapped.notes),
    }


class ToolChoices(BaseModel):
    """`PUT .../tools`. Each field replaces the stored one when sent (an empty list or
    null clears it) and keeps it when omitted."""

    selection: list[OpenAPIOperationRef] | None = None
    overrides: list[OpenAPIOverride] | None = None
    health_probe: OpenAPIOperationRef | None = None


@router.put("/servers/{server_id}/tools", response_model=None)
async def put_tools(
    server_id: str, request: Request, _: None = Depends(auth_dependency)
) -> JSONResponse:
    require_admin(request)
    raw = await _read_capped(request, _TOOLS_BODY_BYTES)
    if raw is None:
        return _error(413, "request too large")
    try:
        choices = ToolChoices.model_validate(json.loads(raw or b"{}"))
    except (json.JSONDecodeError, ValidationError) as exc:
        return _error(400, f"invalid body: {exc}")
    limit = request.app.state.settings.openapi.max_description_chars
    async with request.app.state.openapi_locks(server_id):
        server = await _openapi_server(request, server_id)
        if server is None:
            return _error(404, "no such OpenAPI server")
        try:
            # Mapping a large spec takes most of a second: off the event loop.
            updated = await asyncio.to_thread(
                apply_choices,
                server,
                selection=choices.selection,
                overrides=choices.overrides,
                health_probe=choices.health_probe,
                sent=set(choices.model_fields_set),
                description_limit=limit,
            )
        except MappingError as exc:
            return _error(400, "these choices cannot be served", problems=list(exc.problems))
        await request.app.state.registry.register(updated)
        await request.app.state.discovery_service.discover_immediately(updated)
    stored = await request.app.state.registry.get(server_id)
    return JSONResponse(content={"tools": [tool["name"] for tool in (stored.tools or [])]})
