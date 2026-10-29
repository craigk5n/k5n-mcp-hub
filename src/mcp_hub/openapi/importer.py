"""What an OpenAPI import job does, and the rules for changing a server's tools.

**Specs are loaded only through `openapi.isolation`** (a test asserts this module
never names the in-process loader). Only an import job writes a snapshot: no route
accepts one from a client, because that would skip validation.

Re-importing an existing server follows the same rule as `/v1/register`: a field
sent empty keeps the stored value (`KEPT_ON_REREGISTRATION`), so a script does not
have to resend every secret. The selection, overrides and health probe are kept when
*omitted*, and an explicitly empty value clears them (as ADR 0010 §8 does for tool
policy). A changed spec never takes effect here: it is stored as the pending revision
(ADR 0011 §2) and the live tools keep serving the accepted snapshot.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from mcp_hub.config import Settings
from mcp_hub.models.openapi import OpenAPIOperationRef, OpenAPIOverride, OpenAPISnapshot
from mcp_hub.models.register_request import KEPT_ON_REREGISTRATION
from mcp_hub.models.server import RegisteredServer
from mcp_hub.openapi.arguments import DROPPED_HEADERS
from mcp_hub.openapi.isolation import SpecLimits, load_spec, load_spec_from_url
from mcp_hub.openapi.model import MappingError
from mcp_hub.openapi.server_tools import openapi_preview, openapi_tools
from mcp_hub.utils import utcnow

# An HTTP field-name token (RFC 9110). An API key header outside this would be
# rejected by the client library at call time, far from the registration that set it.
_HEADER_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_PLAIN_PATH = re.compile(r"^/[^?#]*$")
# The importer's own kept fields, beyond those /v1/register keeps.
_KEPT_HERE = (
    "api_key_name",
    "api_key_in",
    "api_key_value",
    "openapi_base_url",
    "name",
    "description",
)


class ServerLocks:
    """One lock per server id, held for each read-modify-write of an OpenAPI
    server's record. Every write replaces the whole record, so without this a
    concurrent import and tool change would each write back the other's stale copy."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def __call__(self, server_id: str) -> asyncio.Lock:
        return self._locks.setdefault(server_id, asyncio.Lock())


class ImportRejected(Exception):
    def __init__(self, message: str, *, problems: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.problems = problems


class OpenAPIServerSettings(BaseModel):
    """A server's settings: `PATCH /v1/openapi/servers/{id}/settings`, and the part
    of an import that is not the spec. Credential fields mirror `/v1/register`'s
    names. Unknown fields are refused, so a settings call cannot smuggle in a spec or
    a selection."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = ""
    name: str = ""
    description: str = ""
    tags: list[str] | None = None
    required_scope: str = ""
    auth_type: Literal["bearer", "basic", "oauth", "obo", "ema", "api_key", ""] = ""
    bearer_token: str = ""
    basic_username: str = ""
    basic_password: str = ""
    oauth_token_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_scope: str = ""
    oauth_resource: str = ""
    obo_audience: str = ""
    obo_resource: str = ""
    obo_scope: str = ""
    obo_actor_token_source: Literal["none", "client_credentials"] = "none"
    ema_resource_as_issuer: str = ""
    ema_resource_as_token_url: str = ""
    ema_resource_id: str = ""
    ema_subject_token_type: Literal["id_token", "access_token"] = "id_token"
    api_key_name: str = ""
    api_key_in: str = ""
    api_key_value: str = ""

    @field_validator("base_url", mode="before")
    @classmethod
    def strip_base_url(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_settings(self) -> OpenAPIServerSettings:
        if self.base_url:
            _validate_base_url(self.base_url)
        if self.auth_type == "api_key" or self.api_key_name or self.api_key_in:
            _validate_api_key(self)
        if self.auth_type == "oauth" and not self.oauth_token_url:
            # No metadata discovery for a REST API: there is no MCP endpoint to
            # discover from, so the token endpoint is stated rather than guessed.
            raise ValueError("auth_type oauth needs oauth_token_url for an OpenAPI server")
        return self

    def sent(self, field: str) -> bool:
        return field in self.model_fields_set


class OpenAPIImportRequest(OpenAPIServerSettings):
    """`POST /v1/openapi/imports`: the settings plus the spec and, optionally, the
    choices. Without a base URL a new server is a draft: imported, with nothing
    exposed, until the admin confirms one (Story 13.8)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    spec: str = ""
    spec_url: str = ""
    selection: list[OpenAPIOperationRef] | None = None
    overrides: list[OpenAPIOverride] | None = None
    health_probe: OpenAPIOperationRef | None = None

    @field_validator("id", "spec_url", mode="before")
    @classmethod
    def strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("id")
    @classmethod
    def id_required(cls, value: str) -> str:
        if not value:
            raise ValueError("id is required")
        return value

    @model_validator(mode="after")
    def validate_spec_source(self) -> OpenAPIImportRequest:
        if bool(self.spec) == bool(self.spec_url):
            raise ValueError("send exactly one of spec (the document) or spec_url")
        return self


def _validate_base_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("base_url must be an absolute http(s) URL")
    # Echoed in previews and listings, and joined with operation paths: credentials
    # belong in the auth fields, and a query or fragment would corrupt every path.
    if parts.username is not None or parts.password is not None:
        raise ValueError("base_url must not contain credentials; use the auth fields")
    if parts.query or parts.fragment or url.endswith(("?", "#")):
        raise ValueError("base_url must not have a query or fragment")


def _validate_api_key(request: OpenAPIServerSettings) -> None:
    if not request.api_key_name:
        raise ValueError("auth_type api_key needs api_key_name")
    if request.api_key_in not in ("header", "query"):
        raise ValueError("api_key_in must be 'header' or 'query'")
    if request.api_key_in == "header" and (
        not _HEADER_TOKEN.fullmatch(request.api_key_name)
        or request.api_key_name.lower() in DROPPED_HEADERS - {"authorization"}
    ):
        raise ValueError(f"api_key_name {request.api_key_name!r} cannot carry an API key header")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in request.api_key_value):
        # A control character is a header-injection attempt or a paste error, and
        # would otherwise fail at call time with the value in the exception.
        raise ValueError("api_key_value must not contain control characters")


# --------------------------------------------------------------------------- the job


async def run_import(
    request: OpenAPIImportRequest,
    *,
    registry: Any,
    discovery_service: Any,
    settings: Settings,
    locks: ServerLocks,
) -> str:
    """Load, check and store. Returns ``created``, ``spec_unchanged`` or
    ``revision_pending``; raises `SpecError`, `MappingError` or `ImportRejected`."""
    limits = SpecLimits.from_config(settings.openapi)
    if request.spec_url:
        loaded = await load_spec_from_url(
            request.spec_url,
            limits=limits,
            allow_private_networks=settings.security.allow_private_networks,
        )
    else:
        loaded = await load_spec(request.spec.encode("utf-8"), limits=limits)
    snapshot = OpenAPISnapshot(
        document=loaded.document,
        dialect=loaded.dialect,
        sha256=loaded.sha256,
        source_url=request.spec_url,
        loaded_at=utcnow(),
    )

    # The slow part (the load) is above; only the read-modify-write is locked.
    async with locks(request.id):
        existing = await registry.get(request.id)
        if existing is not None and not existing.is_openapi:
            raise ImportRejected(f"{request.id!r} is already registered as a non-OpenAPI server")
        if existing is None:
            server, outcome = _new_server(request, snapshot), "created"
        else:
            server, outcome = _updated_server(existing, request, snapshot)
        # Mapping a large spec takes most of a second: off the event loop.
        await asyncio.to_thread(
            check_choices, server, description_limit=settings.openapi.max_description_chars
        )
        await registry.register(server)
        await discovery_service.discover_immediately(server)
    return outcome


def _new_server(request: OpenAPIImportRequest, snapshot: OpenAPISnapshot) -> RegisteredServer:
    fields = {name: getattr(request, name) for name in _CREDENTIAL_FIELDS}
    return RegisteredServer(
        id=request.id,
        url="",
        transport_kind="openapi",
        mcp_transport="openapi",
        openapi_base_url=request.base_url,
        openapi_snapshot=snapshot,
        openapi_selection=request.selection or [],
        openapi_overrides=request.overrides or [],
        openapi_health_probe=request.health_probe,
        name=request.name,
        description=request.description,
        tags=request.tags or [],
        registration_type="manual",
        healthy=True,
        last_checked=utcnow(),
        **fields,
    )


_CREDENTIAL_FIELDS = (
    "required_scope",
    "auth_type",
    "bearer_token",
    "basic_username",
    "basic_password",
    "oauth_token_url",
    "oauth_client_id",
    "oauth_client_secret",
    "oauth_scope",
    "oauth_resource",
    "obo_audience",
    "obo_resource",
    "obo_scope",
    "obo_actor_token_source",
    "ema_resource_as_issuer",
    "ema_resource_as_token_url",
    "ema_resource_id",
    "ema_subject_token_type",
    "api_key_name",
    "api_key_in",
    "api_key_value",
)


def _settings_update(existing: RegisteredServer, request: OpenAPIServerSettings) -> dict[str, Any]:
    """The fields `request` changes on `existing`. A field sent empty keeps the stored
    value (`KEPT_ON_REREGISTRATION` and the importer's own), as on `/v1/register`."""
    incoming = {name: getattr(request, name) for name in _CREDENTIAL_FIELDS}
    incoming.update(
        openapi_base_url=request.base_url, name=request.name, description=request.description
    )
    kept = set(KEPT_ON_REREGISTRATION) | set(_KEPT_HERE)
    update: dict[str, Any] = {
        name: value
        for name, value in incoming.items()
        if value or name not in kept  # empty means "keep" for these
    }
    if request.tags is not None:
        update["tags"] = request.tags
    return update


def apply_settings(
    existing: RegisteredServer, request: OpenAPIServerSettings, *, description_limit: int
) -> RegisteredServer:
    """`PATCH .../settings`: the server with new settings, checked. Raises
    `MappingError` if the result cannot serve its current choices."""
    candidate = existing.model_copy(update=_settings_update(existing, request))
    check_choices(candidate, description_limit=description_limit)
    return candidate


def _updated_server(
    existing: RegisteredServer, request: OpenAPIImportRequest, snapshot: OpenAPISnapshot
) -> tuple[RegisteredServer, str]:
    update = _settings_update(existing, request)
    if request.sent("selection"):
        update["openapi_selection"] = request.selection or []
    if request.sent("overrides"):
        update["openapi_overrides"] = request.overrides or []
    if request.sent("health_probe"):
        update["openapi_health_probe"] = request.health_probe

    accepted = existing.openapi_snapshot
    if accepted is None:
        update["openapi_snapshot"], outcome = snapshot, "created"
    elif accepted.sha256 == snapshot.sha256:
        outcome = "spec_unchanged"
    else:
        update["openapi_pending"], outcome = snapshot, "revision_pending"
    return existing.model_copy(update=update), outcome


# --------------------------------------------------------------------------- choices


def check_choices(server: RegisteredServer, *, description_limit: int) -> None:
    """Raise `MappingError` unless the selection, overrides and probe can be served
    from the accepted snapshot."""
    problems: list[str] = []
    if not server.openapi_base_url and (server.openapi_selection or server.openapi_health_probe):
        problems.append(
            "set a base URL (PATCH /v1/openapi/servers/{id}/settings) before enabling "
            "tools or a health probe"
        )
    try:
        openapi_tools(server, description_limit=description_limit)
    except MappingError as exc:
        problems.extend(exc.problems)
    probe = server.openapi_health_probe
    if probe is not None:
        problem = health_probe_problem(server, probe, description_limit=description_limit)
        if problem:
            problems.append(problem)
    if problems:
        raise MappingError(problems)


def health_probe_problem(
    server: RegisteredServer, probe: OpenAPIOperationRef, *, description_limit: int
) -> str | None:
    """Why `probe` cannot be the health probe, or None. It need not be selected: it
    is the hub's own liveness check, not something exposed to callers."""
    label = f"health probe {probe.method} {probe.path}"
    if probe.method != "GET":
        return f"{label}: the health probe must be a GET operation"
    if (
        not _PLAIN_PATH.fullmatch(probe.path)
        or "/../" in f"{probe.path}/"
        or "/./" in f"{probe.path}/"
    ):
        # Appended to the base URL as-is, so it must not climb out of the base path
        # or smuggle in a query.
        return f"{label}: the health probe must be a plain path (no '..', '.', '?' or '#')"
    mapped = {m.key: m for m in openapi_preview(server, description_limit=description_limit)}
    operation = mapped.get(probe.key())
    if operation is None:
        return f"{label} is not in the spec"
    if operation.tool is None:
        return f"{label} cannot be used: {'; '.join(operation.unsupported)}"
    required = operation.tool.input_schema.get("required", [])
    if required:
        return (
            f"{label} has required parameters ({', '.join(required)}), so it cannot run on its own"
        )
    return None


def apply_choices(
    server: RegisteredServer,
    *,
    selection: list[OpenAPIOperationRef] | None,
    overrides: list[OpenAPIOverride] | None,
    health_probe: OpenAPIOperationRef | None,
    sent: set[str],
    description_limit: int,
) -> RegisteredServer:
    """The server with new choices, checked, for `PUT .../tools`. Omitted fields keep
    their stored value; raises `MappingError` and leaves `server` untouched if the
    result cannot be served."""
    update: dict[str, Any] = {}
    if "selection" in sent:
        update["openapi_selection"] = selection or []
    if "overrides" in sent:
        update["openapi_overrides"] = overrides or []
    if "health_probe" in sent:
        update["openapi_health_probe"] = health_probe
    candidate = server.model_copy(update=update)
    check_choices(candidate, description_limit=description_limit)
    return candidate
