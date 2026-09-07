from typing import Any, Literal

from pydantic import BaseModel, field_validator, model_validator


class RegisterRequest(BaseModel):
    id: str
    # Optional for a stdio server, which has no URL -- one is synthesized as
    # `stdio:<name>` from the allowlist entry. Still required for HTTP.
    url: str = ""
    # A stdio registration names an ALLOWLIST ENTRY, never a command line. There is
    # deliberately no `command`/`args`/`env`/`cwd` field here: the operator supplies
    # those in config, so argument injection is unreachable by construction rather
    # than blocked by validation (ADR 0007).
    transport_kind: Literal["http", "stdio"] = "http"
    stdio_command_name: str = ""
    # Set by the registry import route, not by hand: these say the record was
    # described elsewhere, which is a claim only the importer is in a position to make.
    registry_source: str = ""
    registry_name: str = ""
    registry_version: str = ""
    name: str = ""
    version: str = ""
    description: str = ""
    tags: list[str] = []
    registration_type: Literal["self", "manual", ""] = ""
    mcp_protocol_version: str = ""
    mcp_transport: Literal["http", "sse", ""] = ""
    auth_type: Literal["bearer", "basic", "oauth", "obo", "ema", ""] = ""
    bearer_token: str = ""
    basic_username: str = ""
    basic_password: str = ""
    oauth_discovery_url: str = ""
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
    required_scope: str = ""
    trace_verbose: bool = False

    @field_validator("id", "url", mode="before")
    @classmethod
    def strip_whitespace(cls, v: str | None) -> str | None:
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("id")
    @classmethod
    def validate_id_not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("id is required")
        return v

    @model_validator(mode="after")
    def validate_transport(self) -> "RegisterRequest":
        if self.transport_kind == "stdio":
            if not self.stdio_command_name.strip():
                raise ValueError("stdio_command_name is required for a stdio server")
            # One process, shared by every caller, with credentials fixed at spawn:
            # it cannot act as two users. Accepting `obo` here would silently hand
            # every caller whatever identity the process started with -- the exact
            # escalation on-behalf-of exists to prevent (ADR 0003, ADR 0007).
            if self.auth_type in ("obo", "ema"):
                raise ValueError(
                    f"auth_type {self.auth_type!r} is not available for a stdio server: "
                    "one shared process cannot act on behalf of individual callers. "
                    "Use a service credential, and required_scope to control who may "
                    "reach it."
                )
        elif not self.url:
            raise ValueError("url is required")
        return self

    @field_validator("registration_type")
    @classmethod
    def validate_registration_type(cls, v: str) -> str:
        if v and v not in ("self", "manual"):
            raise ValueError("must be 'self' or 'manual'")
        return v

    @field_validator("auth_type")
    @classmethod
    def validate_auth_type(cls, v: str) -> str:
        if v and v not in ("bearer", "basic", "oauth", "obo", "ema"):
            raise ValueError("must be 'bearer', 'basic', 'oauth', 'obo', or 'ema'")
        return v

    @field_validator("mcp_transport")
    @classmethod
    def validate_mcp_transport(cls, v: str) -> str:
        if v and v not in ("http", "sse"):
            raise ValueError("must be 'http' or 'sse'")
        return v

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v: Any) -> list[str]:
        if not isinstance(v, list):
            raise ValueError("must be a list")
        return [str(item) for item in v]
