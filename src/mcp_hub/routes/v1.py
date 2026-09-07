import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from mcp_hub.mcp.discovery import DiscoveryService
from mcp_hub.mcp.oauth import discover_oauth_metadata, token_endpoint_from_metadata
from mcp_hub.models import RegisteredServer
from mcp_hub.auth.authorize import require_admin
from mcp_hub.utils import sanitize_filename
from mcp_hub.mcp.registry_export import export_warnings, to_server_json
from mcp_hub.mcp.registry_import import stdio_suggestion, to_register_payload
from mcp_hub.models.register_request import RegisterRequest
from mcp_hub.registry.service import Registry
from mcp_hub.utils import is_url_safe_for_discovery, utcnow

MAX_REQUEST_BODY_SIZE = 1024 * 1024  # 1MB

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["v1"])


def get_registry(request: Request) -> Registry:
    return request.app.state.registry


async def auth_dependency(request: Request) -> None:
    auth_required_dep = request.app.state.auth_required_dependency
    await auth_required_dep(request)


def get_discovery_service(request: Request) -> DiscoveryService:
    return request.app.state.discovery_service  # type: ignore[return-value]


@router.post(
    "/register",
    response_model=None,
    status_code=201,
)
async def register_server(
    request: Request,
    registry: Registry = Depends(get_registry),
    discovery_service: DiscoveryService = Depends(get_discovery_service),
    _: None = Depends(auth_dependency),
) -> JSONResponse | PlainTextResponse:
    try:
        return await _register_server_impl(request, registry, discovery_service)
    except Exception:
        logger.exception("Unexpected error in register_server")
        raise


async def _register_server_impl(
    request: Request,
    registry: Registry,
    discovery_service: DiscoveryService,
) -> JSONResponse | PlainTextResponse:
    body = await request.body()
    require_admin(request)
    if len(body) > MAX_REQUEST_BODY_SIZE:
        return PlainTextResponse("request body too large", status_code=400)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return PlainTextResponse("invalid json", status_code=400)

    if not isinstance(data, dict):
        return PlainTextResponse("id and url required", status_code=400)

    return await register_from_data(request, registry, discovery_service, data)


async def register_from_data(
    request: Request,
    registry: Registry,
    discovery_service: DiscoveryService,
    data: dict[str, Any],
) -> JSONResponse | PlainTextResponse:
    """Register from an already-parsed body.

    Split out so registry import shares this path rather than reimplementing it.
    Everything that makes a registration safe lives below here -- SSRF validation of
    the URL, the stdio allowlist check, credential merging on re-registration -- and a
    second implementation would eventually drift from one of them. The caller is
    responsible for `require_admin`, since it also decides what "the caller" means.
    """
    try:
        validated = RegisterRequest.model_validate(data)
    except ValidationError as e:
        errors = e.errors()
        if not errors:
            return PlainTextResponse("validation error", status_code=400)

        for error in errors:
            loc = error.get("loc", [])
            err_type = error.get("type", "")
            msg = error.get("msg", "")
            # `url` is now validated on the model as a whole (a stdio server has no
            # URL), so its error arrives with an empty `loc` rather than loc=("url",).
            # Match on the message too, or the documented "id and url required"
            # response silently becomes ": Value error, url is required".
            if ("id" in loc or "url" in loc or "url is required" in msg) and (
                err_type == "missing" or "is required" in msg
            ):
                return PlainTextResponse("id and url required", status_code=400)

        first_error = errors[0]
        field = ".".join(str(loc) for loc in first_error.get("loc", []))
        msg = first_error.get("msg", "invalid value")
        return PlainTextResponse(f"{field}: {msg}", status_code=400)

    server_id = validated.id
    url = validated.url

    # stdio: the feature must be on, and the request may only name an entry the
    # operator already allowlisted. Both checks live here rather than in the model
    # because they depend on config, and both fail closed.
    if validated.transport_kind == "stdio":
        stdio_cfg = getattr(getattr(request.app.state, "settings", None), "stdio", None)
        if stdio_cfg is None or not stdio_cfg.enabled:
            return PlainTextResponse(
                "stdio servers are disabled: set stdio.enabled (and see ADR 0007 for "
                "why it also requires authenticated registration)",
                status_code=400,
            )
        if validated.stdio_command_name not in stdio_cfg.allowed_commands:
            allowed = ", ".join(sorted(stdio_cfg.allowed_commands)) or "(none configured)"
            return PlainTextResponse(
                f"{validated.stdio_command_name!r} is not an allowed stdio command. "
                f"Allowlisted entries: {allowed}",
                status_code=400,
            )
        url = f"stdio:{validated.stdio_command_name}"

    allow_private = bool(
        getattr(getattr(request.app.state, "settings", None), "security", None)
        and request.app.state.settings.security.allow_private_networks
    )

    existing = await registry.get(server_id)
    incoming_registration_type = validated.registration_type
    effective_registration_type = incoming_registration_type
    if existing is not None and not incoming_registration_type and existing.registration_type:
        effective_registration_type = existing.registration_type
    if not effective_registration_type:
        effective_registration_type = "manual"

    require_reachability = effective_registration_type != "self"

    resolved_ips: list[str] = []
    if validated.transport_kind == "stdio":
        # Nothing to resolve or connect to: the safety boundary for a stdio server is
        # the allowlist checked above, not DNS and IP-range validation.
        pass
    else:
        is_safe, error_msg, resolved_ips = await is_url_safe_for_discovery(
            url, require_reachability, allow_private
        )
        if not is_safe:
            return JSONResponse(
                status_code=400,
                content={"error": "URL validation failed"},
            )

    oauth_discovery_url = validated.oauth_discovery_url
    if oauth_discovery_url:
        is_safe, _, _ = await is_url_safe_for_discovery(
            oauth_discovery_url, require_reachability, allow_private
        )
        if not is_safe:
            return JSONResponse(
                status_code=400,
                content={"error": "URL validation failed"},
            )

    srv = RegisteredServer(
        id=server_id,
        url=url.strip(),
        transport_kind=validated.transport_kind,
        stdio_command_name=validated.stdio_command_name,
        registry_source=validated.registry_source,
        registry_name=validated.registry_name,
        registry_version=validated.registry_version,
        imported_at=utcnow() if validated.registry_name else None,
        healthy=True,
        consecutive_fails=0,
        last_checked=utcnow(),
        registration_type=validated.registration_type,
        auth_type=validated.auth_type,
        bearer_token=validated.bearer_token,
        basic_username=validated.basic_username,
        basic_password=validated.basic_password,
        oauth_discovery_url=oauth_discovery_url,
        oauth_token_url=validated.oauth_token_url,
        oauth_client_id=validated.oauth_client_id,
        oauth_client_secret=validated.oauth_client_secret,
        oauth_scope=validated.oauth_scope,
        oauth_resource=validated.oauth_resource,
        obo_audience=validated.obo_audience,
        obo_resource=validated.obo_resource,
        obo_scope=validated.obo_scope,
        obo_actor_token_source=validated.obo_actor_token_source,
        ema_resource_as_issuer=validated.ema_resource_as_issuer,
        ema_resource_as_token_url=validated.ema_resource_as_token_url,
        ema_resource_id=validated.ema_resource_id,
        ema_subject_token_type=validated.ema_subject_token_type,
        required_scope=validated.required_scope,
        name=validated.name,
        version=validated.version,
        description=validated.description,
        tags=validated.tags,
        mcp_protocol_version=validated.mcp_protocol_version,
        mcp_transport=validated.mcp_transport,
        trace_verbose=validated.trace_verbose,
    )

    if existing is not None:
        if not srv.registration_type and existing.registration_type:
            srv.registration_type = existing.registration_type
    if not srv.registration_type:
        srv.registration_type = "manual"

    if not srv.auth_type:
        if srv.bearer_token:
            srv.auth_type = "bearer"
        elif srv.basic_username or srv.basic_password:
            srv.auth_type = "basic"
        elif srv.oauth_discovery_url or srv.oauth_token_url:
            srv.auth_type = "oauth"

    merge_fields = [
        "auth_type",
        "obo_audience",
        "obo_resource",
        "obo_scope",
        "obo_actor_token_source",
        "ema_resource_as_issuer",
        "ema_resource_as_token_url",
        "ema_resource_id",
        "ema_subject_token_type",
        "required_scope",
        "bearer_token",
        "basic_username",
        "basic_password",
        "oauth_discovery_url",
        "oauth_token_url",
        "oauth_client_id",
        "oauth_client_secret",
        "oauth_scope",
        "oauth_resource",
    ]

    if existing is not None:
        for field in merge_fields:
            incoming_val = getattr(srv, field)
            if not incoming_val:
                existing_val = getattr(existing, field)
                setattr(srv, field, existing_val)

    if srv.registration_type == "self":
        await registry.register(srv)
        return JSONResponse(
            status_code=201,
            content=srv.sanitize_for_api().model_dump(mode="json"),
        )

    is_new_registration = existing is None

    if srv.auth_type == "oauth" or srv.oauth_discovery_url:
        oauth_discovery_failed = False
        try:
            discovery_url_for_call = srv.oauth_discovery_url if srv.oauth_discovery_url else ""
            disc_url, issuer, metadata = await discover_oauth_metadata(
                srv.url, discovery_url_for_call, allow_private_networks=allow_private
            )
            is_safe, _, _ = await is_url_safe_for_discovery(disc_url, allow_private=allow_private)
            if is_safe:
                srv.oauth_discovery_url = disc_url
                srv.oauth_issuer = issuer
                srv.oauth_metadata = metadata
                srv.oauth_last_checked = utcnow()

                if not srv.oauth_token_url:
                    srv.oauth_token_url = token_endpoint_from_metadata(metadata)
        except Exception:
            oauth_discovery_failed = True
            logger.warning(
                "OAuth discovery failed for %s",
                srv.id,
            )

        if oauth_discovery_failed:
            if is_new_registration:
                return JSONResponse(
                    status_code=400,
                    content={"error": "oauth discovery failed"},
                )
            return JSONResponse(
                status_code=400,
                content={"error": "oauth discovery failed"},
            )

    await registry.register(srv)

    # Discover capabilities in the background so registration returns immediately. A slow or
    # hanging backend can no longer block the Add request (which manifested as an "HTTP 0"
    # dropped connection in the browser). The server stays registered regardless; its
    # capabilities and MCP metadata populate asynchronously, and the background health checker
    # keeps its health fresh. Discovery is bounded by its own (now-enforced) timeout, and its
    # failure is logged rather than surfaced as a registration error.
    async def _background_discover() -> None:
        try:
            await discovery_service.discover_immediately(srv, timeout=20)
        except Exception as discovery_error:
            logger.warning("Background discovery failed for %s: %s", srv.id, discovery_error)

    discovery_task = asyncio.create_task(_background_discover())
    context = getattr(request.app.state, "context", None)
    if context is not None:
        # Track the task so it's cancelled cleanly on shutdown.
        context.background_tasks.append(discovery_task)

    return JSONResponse(
        status_code=201,
        content=srv.sanitize_for_api().model_dump(mode="json"),
    )


@router.get("/servers", response_model=list[RegisteredServer])
async def list_servers(
    registry: Registry = Depends(get_registry),
) -> list[RegisteredServer]:
    servers = await registry.list()
    return [s.sanitize_for_api() for s in servers]


@router.post("/registry/import", response_model=None)
async def import_from_registry(
    request: Request,
    registry: Registry = Depends(get_registry),
    discovery_service: DiscoveryService = Depends(get_discovery_service),
    _: None = Depends(auth_dependency),
) -> JSONResponse | PlainTextResponse:
    """Register a server the operator picked out of the MCP registry.

    Importing *is* registering, so this goes through `register_from_data` rather than
    writing to storage: the imported URL gets the same `is_url_safe_for_discovery`
    check a typed one does, and credentials already stored survive a re-import. A
    registry record is attacker-influenceable input naming a URL the hub will then
    probe on a timer, so a side door around those checks is exactly what this must not
    be. The hub never publishes in the other direction -- see ADR 0008.
    """
    require_admin(request)

    try:
        body = json.loads(await request.body())
    except json.JSONDecodeError:
        return PlainTextResponse("invalid json", status_code=400)
    if not isinstance(body, dict):
        return PlainTextResponse("name required", status_code=400)

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return PlainTextResponse("name required", status_code=400)

    client = getattr(request.app.state, "registry_client", None)
    if client is None:
        return PlainTextResponse("registry client unavailable", status_code=503)

    try:
        record = await client.get(name.strip(), body.get("version") or "latest")
    except Exception as e:
        logger.warning("registry lookup failed for %s: %s", name, e)
        return PlainTextResponse(f"registry lookup failed: {e}", status_code=502)

    if record is None:
        return PlainTextResponse(f"no registry record named {name!r}", status_code=404)

    suggestion = stdio_suggestion(record)
    if suggestion is not None:
        # A package-based record names a program. ADR 0007 keeps commands in operator
        # config, so the answer is instructions rather than a registration.
        return PlainTextResponse(suggestion.instruction, status_code=400)

    try:
        remote_index = int(body.get("remote_index") or 0)
        payload = to_register_payload(record, remote_index=remote_index)
        # From settings, not from the client object: it is the configured answer to
        # "which registry", it is always present, and reading it off the client made
        # the field silently empty whenever the client did not happen to expose it.
        settings = getattr(request.app.state, "settings", None)
        payload["registry_source"] = getattr(getattr(settings, "registry", None), "base_url", "")
        payload["registry_name"] = record.name
        payload["registry_version"] = record.version
    except (ValueError, TypeError) as e:
        return PlainTextResponse(str(e), status_code=400)

    return await register_from_data(request, registry, discovery_service, payload)


@router.get("/servers/{server_id:path}/server.json", response_model=None)
async def export_server_json(
    request: Request,
    server_id: str,
    registry: Registry = Depends(get_registry),
    _: None = Depends(auth_dependency),
) -> JSONResponse | PlainTextResponse:
    """A registry-shaped `server.json` for this server, to review and publish yourself.

    The hub does not publish (ADR 0008): its records describe this deployment, the
    public index is append-only, and publishing needs a namespace only the operator can
    prove they own. So the deliverable is a file and a list of things to look at, with
    the review step left where it belongs.

    Admin-only. The document carries no credential, but it does describe what this hub
    proxies, which is not something every authenticated caller should be able to
    enumerate into a file.
    """
    require_admin(request)

    srv = await registry.get(server_id)
    if srv is None:
        return PlainTextResponse("Server not found", status_code=404)

    document = to_server_json(srv)
    warnings = export_warnings(srv)

    headers = {
        "Content-Disposition": f'attachment; filename="{sanitize_filename(server_id)}.server.json"'
    }
    if warnings:
        # Also returned in the body's sibling field below; the header is for anyone
        # scripting this, who would otherwise have to parse the file to notice.
        headers["X-Export-Warnings"] = str(len(warnings))

    return JSONResponse(
        content={"server": document, "warnings": warnings},
        headers=headers,
    )
