from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerConfig(BaseModel):
    model_config = SettingsConfigDict(populate_by_name=True)

    http_host: str = "127.0.0.1"
    http_port: int = 8080
    admin_ui: bool = True


class RedisConfig(BaseModel):
    addr: str = ""
    password: str = ""
    db: int = 0
    ttl_seconds: int = 0


class JSONStorageConfig(BaseModel):
    model_config = SettingsConfigDict(populate_by_name=True)

    path: str = "mcp_servers.json"


StorageType = Literal["inmemory", "json", "jsonfile", "file", "redis"]


class StorageConfig(BaseModel):
    model_config = SettingsConfigDict(populate_by_name=True)

    type: StorageType = "inmemory"
    redis: RedisConfig = RedisConfig()
    json_: JSONStorageConfig = Field(default_factory=JSONStorageConfig, alias="json")

    @field_validator("type", mode="before")
    @classmethod
    def normalize_storage_type(cls, v: str | None) -> str:
        if v is None:
            return "inmemory"
        normalized = v.lower()
        if normalized in ("json", "jsonfile", "file"):
            return "json"
        return normalized

    @model_validator(mode="after")
    def validate_redis_not_implemented(self) -> StorageConfig:
        if self.type == "redis":
            pass
        return self


class BasicAuthConfig(BaseModel):
    register_user: str = "admin"
    register_pass: str = "admin123"


class AuthConfig(BaseModel):
    model_config = SettingsConfigDict(populate_by_name=True)

    type: str = "basic"
    basic_auth: BasicAuthConfig = Field(default_factory=BasicAuthConfig)

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        valid_types = {"basic", "none", "noauth", ""}
        if v not in valid_types:
            raise ValueError(
                f"Invalid auth type: {v}. Must be one of: {', '.join(sorted(valid_types))}"
            )
        return v


class HealthResponseFields(BaseModel):
    status: str = "status"
    uptime_seconds: str = "uptime_seconds"


class HealthCheckConfig(BaseModel):
    enabled: bool = False
    interval_seconds: int = 30
    timeout_seconds: int = 5
    failure_threshold: int = 3
    auto_unregister: bool = False
    response_fields: HealthResponseFields = Field(default_factory=HealthResponseFields)

    @field_validator("interval_seconds", mode="before")
    @classmethod
    def validate_interval(cls, v: int | None) -> int:
        if v is not None and v <= 0:
            return 30
        return v if v is not None else 30

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def validate_timeout(cls, v: int | None) -> int:
        if v is not None and v <= 0:
            return 5
        return v if v is not None else 5

    @field_validator("failure_threshold", mode="before")
    @classmethod
    def validate_failure_threshold(cls, v: int | None) -> int:
        if v is not None and v <= 0:
            return 3
        return v if v is not None else 3


class TraceConfig(BaseModel):
    body_limit: int = 10000
    capture_sse: bool = False


class SecurityConfig(BaseModel):
    # allow_private_networks: permit registering/probing MCP servers on loopback/LAN/private
    # IPs (127.0.0.1, 192.168.x, 10.x, 172.16-31.x, link-local). The SSRF guard blocks these
    # by default; a local-first hub that manages localhost/LAN servers needs them enabled.
    # Keep False if the hub is ever exposed to untrusted callers.
    allow_private_networks: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DEVHUB_",
        populate_by_name=True,
        case_sensitive=False,
    )

    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    healthcheck: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)

    @classmethod
    def from_defaults(cls) -> Settings:
        return cls(
            server=ServerConfig(),
            storage=StorageConfig(),
            auth=AuthConfig(),
            healthcheck=HealthCheckConfig(),
            trace=TraceConfig(),
            security=SecurityConfig(),
        )


def _load_yaml_config(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        path = Path.cwd() / "config.yaml"
    else:
        path = Path(path)

    if not path.exists():
        return {}

    with open(path) as f:
        return yaml.safe_load(f) or {}


def _transform_env_key(key: str) -> str:
    key = key.upper()
    key = key.replace("__", ".")
    return key


def _collect_nested_env_vars(prefix: str = "DEVHUB_") -> dict[str, Any]:
    result: dict[str, Any] = {}
    prefix_len = len(prefix)

    for env_key, env_value in os.environ.items():
        if not env_key.startswith(prefix):
            continue

        key = env_key[prefix_len:]
        if not key:
            continue

        parts = key.lower().split("__")
        current = result

        for i, part in enumerate(parts[:-1]):
            if part not in current:
                current[part] = {}
            current = current[part]

        current[parts[-1]] = env_value

    return result


def load_settings(path: str | None = None) -> Settings:
    defaults = Settings.from_defaults()

    yaml_config = _load_yaml_config(path)

    nested_env_vars = _collect_nested_env_vars()

    server_dict = {
        **defaults.server.model_dump(),
        **yaml_config.get("server", {}),
        **nested_env_vars.get("server", {}),
    }
    storage_dict = {
        **defaults.storage.model_dump(),
        **yaml_config.get("storage", {}),
        **nested_env_vars.get("storage", {}),
    }
    auth_dict = {
        **defaults.auth.model_dump(),
        **yaml_config.get("auth", {}),
        **nested_env_vars.get("auth", {}),
    }
    healthcheck_dict = {
        **defaults.healthcheck.model_dump(),
        **yaml_config.get("healthcheck", {}),
        **nested_env_vars.get("healthcheck", {}),
    }
    trace_dict = {
        **defaults.trace.model_dump(),
        **yaml_config.get("trace", {}),
        **nested_env_vars.get("trace", {}),
    }
    security_dict = {
        **defaults.security.model_dump(),
        **yaml_config.get("security", {}),
        **nested_env_vars.get("security", {}),
    }

    settings = Settings(
        server=ServerConfig(**server_dict),
        storage=StorageConfig(**storage_dict),
        auth=AuthConfig(**auth_dict),
        healthcheck=HealthCheckConfig(**healthcheck_dict),
        trace=TraceConfig(**trace_dict),
        security=SecurityConfig(**security_dict),
    )

    bare_http_port = os.environ.get("SERVER_HTTP_PORT")
    if bare_http_port:
        port_value = int(bare_http_port)
        if port_value != 0:
            settings.server.http_port = port_value

    return settings
