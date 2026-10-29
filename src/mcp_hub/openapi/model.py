"""What mapping produces: tool definitions, and the plan for turning a call into a
request (Story 13.3). Story 13.5 executes `RequestPlan`; nothing here does I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class OperationKey:
    """`(METHOD, path template)`: what selections and overrides are keyed by. Not the
    operationId, which is optional and renamed more often than routes (ADR 0011 §3)."""

    method: str
    path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", self.method.upper())

    def __str__(self) -> str:
        return f"{self.method} {self.path}"


@dataclass(frozen=True)
class Override:
    """An admin's replacement name and/or description for one tool (ADR 0011 §5)."""

    name: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class ParameterPlan:
    """One tool argument that becomes a path, query or header parameter."""

    argument: str
    name: str
    location: Literal["path", "query", "header"]
    style: str
    explode: bool


@dataclass(frozen=True)
class BodyPlan:
    """How arguments become the request body.

    ``merged``: each ``(argument, property)`` in `fields` is one property of an object
    body (renamed ``body_<name>`` where it collided with a parameter). ``whole``: the
    single argument in `fields` (property ``""``) *is* the body.
    """

    media_type: str
    mode: Literal["merged", "whole"]
    fields: tuple[tuple[str, str], ...]
    required: bool


@dataclass(frozen=True)
class RequestPlan:
    method: str
    path: str
    parameters: tuple[ParameterPlan, ...]
    body: BodyPlan | None


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    annotations: dict[str, Any]

    def to_mcp(self) -> dict[str, Any]:
        """The tool as tools/list returns it."""
        tool: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.output_schema is not None:
            tool["outputSchema"] = self.output_schema
        if self.annotations:
            tool["annotations"] = self.annotations
        return tool


@dataclass(frozen=True)
class Tool:
    """A selected, servable operation: what tools/list shows and tools/call runs."""

    key: OperationKey
    definition: ToolDef
    plan: RequestPlan


@dataclass(frozen=True)
class MappedOperation:
    """One operation as the admin preview shows it.

    `tool` and `plan` are None when the operation is `unsupported`: it is listed, with
    reasons, but cannot be enabled. `dropped` lists parameters that were left out of
    the tool on purpose (credential headers, cookies). `problems` are things the
    admin can fix with an override: an invalid override name, or a name another
    operation also has.
    """

    key: OperationKey
    tool: ToolDef | None
    plan: RequestPlan | None
    unsupported: tuple[str, ...]
    dropped: tuple[str, ...]
    selected: bool
    name_problem: str | None = None
    conflicts: tuple[OperationKey, ...] = field(default=())

    @property
    def problems(self) -> tuple[str, ...]:
        found = [self.name_problem] if self.name_problem else []
        if self.conflicts and self.tool is not None:
            others = ", ".join(str(key) for key in self.conflicts)
            found.append(f"tool name '{self.tool.name}' is also used by {others}")
        return tuple(found)


class MappingError(Exception):
    """The selection cannot be served. `problems` lists every reason, not the first."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = tuple(problems)
        super().__init__(
            "The selected operations cannot be served:\n"
            + "\n".join(f"- {problem}" for problem in problems)
        )
