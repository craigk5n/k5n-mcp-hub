from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator


AGENT_DATETIME_FIELDS = (
    "created_at",
    "updated_at",
    "last_card_checked",
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


class AgentCard(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    description: str
    version: str
    url: str
    capabilities: dict[str, Any] = {}
    skills: list[dict[str, Any]] = []
    default_input_modes: list[str] = []
    default_output_modes: list[str] = []
    auth: dict[str, Any] | None = None


class RegisteredAgent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    url: str
    name: str = ""
    description: str = ""
    tags: list[str] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None
    bearer_token: str = ""

    last_card: AgentCard | None = None
    last_card_checked: datetime | None = None
    card_valid: bool | None = None
    card_issues: list[str] = []

    @field_validator(
        *AGENT_DATETIME_FIELDS,
        mode="before",
    )
    @classmethod
    def parse_datetime(cls, v: datetime | str | None) -> datetime | None:
        if isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc)
        return _utc_datetime_deserializer(v)

    @field_serializer(*AGENT_DATETIME_FIELDS)
    def serialize_datetime(self, dt: datetime | None) -> str | None:
        return _utc_datetime_serializer(dt)

    def sanitize_for_api(self) -> "RegisteredAgent":
        return self.model_copy(
            update={
                "bearer_token": "",
            }
        )

    def sanitize_for_persistence(self) -> "RegisteredAgent":
        return self.model_copy(
            update={
                "bearer_token": "",
            }
        )
