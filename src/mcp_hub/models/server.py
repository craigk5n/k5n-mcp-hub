from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


DATETIME_FIELDS = (
    "created_at",
    "updated_at",
    "oauth_last_checked",
    "last_checked",
    "last_capability_sync",
    "healthy_since",
)


def _utc_datetime_deserializer(dt_str: str | None) -> datetime | None:
    if dt_str is None:
        return None
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _utc_datetime_serializer(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


class FaultInjection(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = False
    timeout_enabled: bool = False
    timeout_millis: int = 0
    malformed_json: bool = False
    invalid_method: bool = False
    sse_interrupt: bool = False


class RegisteredServer(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    url: str
    name: str = ""
    version: str = ""
    description: str = ""
    tags: list[str] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None
    registration_type: Literal["self", "manual", ""] = ""
    mcp_protocol_version: str = ""
    mcp_transport: Literal["http", "sse", ""] = ""
    mcp_conformant: bool | None = None
    auth_type: Literal["bearer", "basic", "oauth", "obo", ""] = ""
    bearer_token: str = ""
    basic_username: str = ""
    basic_password: str = ""
    oauth_discovery_url: str = ""
    oauth_issuer: str = ""
    oauth_token_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_scope: str = ""
    oauth_resource: str = ""
    oauth_metadata: dict[str, Any] | None = None
    oauth_last_checked: datetime | None = None
    # On-behalf-of (RFC 8693). The hub's own client_id/secret above authenticate the
    # exchange; these describe what the exchanged token should be good for.
    obo_audience: str = ""
    obo_resource: str = ""
    obo_scope: str = ""
    # Delegation is opt-in per server (ADR 0002): most issuers, Keycloak included,
    # do not accept an actor token on the supported exchange path.
    obo_actor_token_source: Literal["none", "client_credentials"] = "none"
    trace_verbose: bool = False
    fault_injection: FaultInjection = Field(default_factory=FaultInjection)

    healthy: bool = False
    # Reachable but the server answered a health probe with HTTP 429 — up, yet throttling the
    # hub (a "degraded" state). `healthy` stays True so it isn't treated as down/unregistered.
    rate_limited: bool = False
    consecutive_fails: int = 0
    last_checked: datetime | None = None
    # When the server most recently became healthy; uptime is derived from this (hub-tracked),
    # reset to None on any failed check.
    healthy_since: datetime | None = None
    uptime_seconds: float = 0.0
    supports_health_endpoint: bool | None = None
    schema_conformant: bool | None = None
    schema_issues: list[str] = []
    oauth_token_status: Literal["ok", "error", ""] = ""
    oauth_token_error: str = ""
    obo_status: Literal["ok", "error", ""] = ""
    obo_error: str = ""
    tools: list[Any] | None = None
    prompts: list[Any] | None = None
    resources: list[Any] | None = None
    last_capability_sync: datetime | None = None

    @field_validator(
        *DATETIME_FIELDS,
        mode="before",
    )
    @classmethod
    def parse_datetime(cls, v: datetime | str | None) -> datetime | None:
        if isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc)
        return _utc_datetime_deserializer(v)

    @field_serializer(*DATETIME_FIELDS)
    def serialize_datetime(self, dt: datetime | None) -> str | None:
        return _utc_datetime_serializer(dt)

    def record_protocol_metadata(
        self,
        protocol_version: str | None,
        transport: Literal["http", "sse", ""] | None = None,
    ) -> bool:
        """Record a negotiated/advertised MCP protocol version (and optionally the
        transport), keeping ``mcp_conformant`` in sync.

        The single write path for this trio — discovery, the background UI probe,
        the Initialize panel, and Invoke all funnel through here so the fields
        can't drift. Returns True when anything changed (callers persist then)."""
        from mcp_hub.mcp.constants import is_supported_protocol_version

        changed = False
        version = (protocol_version or "").strip()
        if version:
            if self.mcp_protocol_version != version:
                self.mcp_protocol_version = version
                changed = True
            conformant = is_supported_protocol_version(version)
            if self.mcp_conformant is not conformant:
                self.mcp_conformant = conformant
                changed = True
        if transport and self.mcp_transport != transport:
            self.mcp_transport = transport
            changed = True
        return changed

    @property
    def has_service_credential(self) -> bool:
        """Whether the hub can reach this backend as itself, with no user in scope.

        Background paths (health, discovery) run on a timer and have no principal, so
        an on-behalf-of server configured *only* for OBO has nothing they can
        authenticate with, and they degrade rather than fail (ADR 0004)."""
        from mcp_hub.mcp.oauth import token_endpoint_from_metadata

        if self.bearer_token.strip():
            return True
        if self.basic_username.strip() or self.basic_password.strip():
            return True
        has_token_endpoint = bool(
            self.oauth_token_url or token_endpoint_from_metadata(self.oauth_metadata)
        )
        return bool(self.oauth_client_id and self.oauth_client_secret and has_token_endpoint)

    @property
    def needs_user_identity(self) -> bool:
        """An on-behalf-of server the background paths cannot act for at all."""
        return self.auth_type == "obo" and not self.has_service_credential

    def sanitize_for_api(self) -> "RegisteredServer":
        return self.model_copy(
            update={
                "bearer_token": "",
                "basic_password": "",
                "oauth_client_secret": "",
                "oauth_token_error": "",
                # Carries the IdP's error text, which can echo token material.
                "obo_error": "",
            }
        )

    def sanitize_for_persistence(self) -> "RegisteredServer":
        return self.model_copy(
            update={
                "tools": None,
                "prompts": None,
                "resources": None,
                "last_capability_sync": None,
                "schema_conformant": None,
                "schema_issues": [],
                "healthy": False,
                "rate_limited": False,
                "last_checked": None,
                "healthy_since": None,
                "uptime_seconds": 0.0,
                # supports_health_endpoint is intentionally NOT reset: once we learn a server
                # has no /health endpoint (404), we remember it so we never probe /health
                # again (even across restarts) and go straight to an MCP ping.
                "oauth_token_status": "",
                "oauth_token_error": "",
                "obo_status": "",
                "obo_error": "",
            }
        )
