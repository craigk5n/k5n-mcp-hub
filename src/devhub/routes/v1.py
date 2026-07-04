import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from devhub.mcp.discovery import DiscoveryService
from devhub.mcp.oauth import discover_oauth_metadata, token_endpoint_from_metadata
from devhub.models import RegisteredServer
from devhub.models.register_request import RegisterRequest
from devhub.registry.service import Registry
from devhub.utils import is_url_safe_for_discovery, utcnow

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
    if len(body) > MAX_REQUEST_BODY_SIZE:
        return PlainTextResponse("request body too large", status_code=400)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return PlainTextResponse("invalid json", status_code=400)

    if not isinstance(data, dict):
        return PlainTextResponse("id and url required", status_code=400)

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
            if ("id" in loc or "url" in loc) and (err_type == "missing" or "is required" in msg):
                return PlainTextResponse("id and url required", status_code=400)

        first_error = errors[0]
        field = ".".join(str(loc) for loc in first_error.get("loc", []))
        msg = first_error.get("msg", "invalid value")
        return PlainTextResponse(f"{field}: {msg}", status_code=400)

    server_id = validated.id
    url = validated.url

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

    is_safe, error_msg, resolved_ips = await is_url_safe_for_discovery(url, require_reachability, allow_private)
    if not is_safe:
        return JSONResponse(
            status_code=400,
            content={"error": "URL validation failed"},
        )

    oauth_discovery_url = validated.oauth_discovery_url
    if oauth_discovery_url:
        is_safe, _, _ = await is_url_safe_for_discovery(oauth_discovery_url, require_reachability, allow_private)
        if not is_safe:
            return JSONResponse(
                status_code=400,
                content={"error": "URL validation failed"},
            )

    srv = RegisteredServer(
        id=server_id,
        url=url.strip(),
        healthy=True,
        consecutive_fails=0,
        last_checked=utcnow(),
        registration_type=validated.registration_type,
        auth_type=validated.auth_type,
        bearer_token=validated.bearer_token,
        oauth_discovery_url=oauth_discovery_url,
        oauth_token_url=validated.oauth_token_url,
        oauth_client_id=validated.oauth_client_id,
        oauth_client_secret=validated.oauth_client_secret,
        oauth_scope=validated.oauth_scope,
        oauth_resource=validated.oauth_resource,
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
        elif srv.oauth_discovery_url or srv.oauth_token_url:
            srv.auth_type = "oauth"

    merge_fields = [
        "auth_type",
        "bearer_token",
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
                srv.url, discovery_url_for_call
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

    try:
        await discovery_service.discover_immediately(srv, timeout=30)
    except Exception:
        logger.warning(
            "Discovery failed for %s",
            srv.id,
        )
        try:
            await registry.unregister(srv.id)
        except Exception as unregister_error:
            logger.error(
                "Failed to unregister %s after discovery failure: %s",
                srv.id,
                unregister_error,
            )

        return JSONResponse(
            status_code=400,
            content={"error": "discovery failed"},
        )

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
