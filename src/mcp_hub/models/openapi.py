"""The OpenAPI parts of a `RegisteredServer` record (ADR 0011 §2-§5, Story 13.4).

Stored inline on the record, so they persist wherever the registry does. Operations
are referenced by `(METHOD, path template)` (ADR 0011 §3), which is what carries a
selection across a spec revision.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, field_validator

from mcp_hub.openapi.model import OperationKey


class OpenAPIOperationRef(BaseModel):
    method: str
    path: str

    @field_validator("method")
    @classmethod
    def upper_case(cls, value: str) -> str:
        return value.upper()

    def key(self) -> OperationKey:
        return OperationKey(self.method, self.path)


class OpenAPIOverride(OpenAPIOperationRef):
    """An admin's name and/or description for one operation's tool."""

    name: str | None = None
    description: str | None = None


class OpenAPISnapshot(BaseModel):
    """A spec as the isolated loader accepted it (`openapi.isolation`).

    `document` is the parsed spec with its local refs in place, in its own dialect
    (Swagger 2.0 is normalized when mapped, not when stored, so a revision diff reads
    in the spec author's terms). `sha256` is over a canonical serialization, so the
    same content reformatted is the same snapshot. Only the import job writes one: no
    route accepts a snapshot from a client, because that would skip validation.
    """

    document: dict[str, Any]
    dialect: Literal["2.0", "3.0", "3.1", "3.2"]
    sha256: str
    source_url: str = ""
    loaded_at: datetime | None = None
