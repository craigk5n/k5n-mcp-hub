from typing import Any, Literal

from pydantic import BaseModel, field_validator


class RegisterRequest(BaseModel):
    id: str
    url: str
    name: str = ""
    version: str = ""
    description: str = ""
    tags: list[str] = []
    registration_type: Literal["self", "manual", ""] = ""
    mcp_protocol_version: str = ""
    mcp_transport: Literal["http", "sse", ""] = ""
    auth_type: Literal["bearer", "basic", "oauth", "obo", ""] = ""
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

    @field_validator("url")
    @classmethod
    def validate_url_not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("url is required")
        return v

    @field_validator("registration_type")
    @classmethod
    def validate_registration_type(cls, v: str) -> str:
        if v and v not in ("self", "manual"):
            raise ValueError("must be 'self' or 'manual'")
        return v

    @field_validator("auth_type")
    @classmethod
    def validate_auth_type(cls, v: str) -> str:
        if v and v not in ("bearer", "basic", "oauth", "obo"):
            raise ValueError("must be 'bearer', 'basic', 'oauth', or 'obo'")
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
