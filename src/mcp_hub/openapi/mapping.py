"""OpenAPI operations to MCP tools (Story 13.3). Pure: no I/O, input never modified.

Two entry points over one mapping, so the admin UI, tools/list and tools/call can
never disagree about what an operation becomes:

- `preview` describes **every** operation, selected or not, and never raises. The
  UI shows its unsupported reasons, dropped parameters and name problems.
- `build_tools` returns the **selected** operations as `Tool`s and raises
  `MappingError` if any of them cannot be served: unsupported, an invalid override
  name, or a name another selected tool also has. A selection that names an
  operation no longer in the spec is skipped; reporting those is Story 13.7's.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Mapping
from typing import Any, Literal

from mcp_hub.openapi.arguments import operation_arguments, resolve_media_schema
from mcp_hub.openapi.model import (
    MappedOperation,
    MappingError,
    OperationKey,
    Override,
    RequestPlan,
    Tool,
    ToolDef,
)
from mcp_hub.openapi.refs import resolve_local
from mcp_hub.openapi.schema_convert import SchemaBundler, deref
from mcp_hub.openapi.swagger2 import UNSUPPORTED, swagger2_to_openapi3
from mcp_hub.openapi.validation import unchecked_patterns

_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
# The MCP spec's tool-name guidance: 1-128 characters, case-sensitive, from this set.
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_NOT_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_NAME = 128
_ANNOTATIONS: dict[str, dict[str, bool]] = {
    "GET": {"readOnlyHint": True},
    "HEAD": {"readOnlyHint": True},
    "DELETE": {"destructiveHint": True},
    "PUT": {"idempotentHint": True},
}


def preview(
    document: dict[str, Any],
    dialect: str,
    *,
    overrides: Mapping[OperationKey, Override],
    description_limit: int,
    selection: frozenset[OperationKey] = frozenset(),
) -> tuple[MappedOperation, ...]:
    """Every operation in the spec, as the admin preview shows it."""
    if dialect == "2.0":
        document, dialect = swagger2_to_openapi3(document), "3.0"
    mapped = [
        _map_operation(
            document, dialect, key, item, op, overrides.get(key), description_limit, selection
        )
        for key, item, op in _operations(document)
    ]
    return tuple(_with_conflicts(mapped))


def build_tools(
    document: dict[str, Any],
    dialect: str,
    *,
    selection: frozenset[OperationKey],
    overrides: Mapping[OperationKey, Override],
    description_limit: int,
) -> tuple[Tool, ...]:
    """The selected operations as servable tools, or `MappingError` saying why not."""
    chosen = [
        m
        for m in preview(
            document,
            dialect,
            overrides=overrides,
            description_limit=description_limit,
            selection=selection,
        )
        if m.selected
    ]
    problems: list[str] = []
    for mapped in chosen:
        if mapped.tool is None:
            problems.append(f"{mapped.key} cannot be enabled: {'; '.join(mapped.unsupported)}")
        elif mapped.name_problem:
            problems.append(f"{mapped.key}: {mapped.name_problem}")
    # Conflicts again, among the selected only: a clash with an operation nobody
    # enabled is a preview warning, not a reason to refuse.
    tools: list[Tool] = []
    for mapped in _with_conflicts([m for m in chosen if m.tool is not None]):
        if mapped.tool is None or mapped.plan is None:
            continue
        if mapped.conflicts:
            problems.append(f"{mapped.key}: {mapped.problems[-1]}")
        tools.append(Tool(key=mapped.key, definition=mapped.tool, plan=mapped.plan))
    if problems:
        raise MappingError(problems)
    return tuple(tools)


def _operations(
    document: dict[str, Any],
) -> Iterator[tuple[OperationKey, dict[str, Any], dict[str, Any]]]:
    for path, item in document.get("paths", {}).items():
        if path.startswith("x-") or not isinstance(item, dict):
            continue
        if isinstance(item.get("$ref"), str):
            item = resolve_local(document, item["$ref"])
        for method in _METHODS:
            if isinstance(item.get(method), dict):
                yield OperationKey(method, path), item, item[method]


def _map_operation(
    document: dict[str, Any],
    dialect: str,
    key: OperationKey,
    item: dict[str, Any],
    operation: dict[str, Any],
    override: Override | None,
    description_limit: int,
    selection: frozenset[OperationKey],
) -> MappedOperation:
    input_bundle = SchemaBundler(document, dialect)
    args = operation_arguments(document, input_bundle, key.path, item, operation)
    unsupported = [*operation.get(UNSUPPORTED, []), *args.unsupported]
    if key.method == "TRACE":
        unsupported.insert(
            0,
            "TRACE echoes the request back, including the credential the hub adds, so it "
            "is never exposed",
        )
    if unsupported:
        return MappedOperation(
            key, None, None, tuple(unsupported), tuple(args.dropped), key in selection
        )
    name, name_problem = _tool_name(key, operation, override)
    output_schema, output_kind = _output_schema(document, dialect, operation)
    root: dict[str, Any] = {"type": "object", "properties": args.properties}
    if args.required:
        root["required"] = args.required
    root["additionalProperties"] = False
    tool = ToolDef(
        name=name,
        description=_description(key, operation, override, description_limit),
        input_schema=copy.deepcopy(input_bundle.finish(root)),
        output_schema=output_schema,
        annotations=dict(_ANNOTATIONS.get(key.method, {})),
    )
    plan = RequestPlan(key.method, key.path, tuple(args.parameters), args.body, output_kind)
    return MappedOperation(
        key,
        tool,
        plan,
        (),
        tuple(args.dropped),
        key in selection,
        name_problem=name_problem,
        notes=tuple(
            f"pattern '{p}' cannot be checked by the hub (backreferences and lookaround "
            "are not supported); the API itself will enforce it"
            for p in unchecked_patterns(tool.input_schema)
        ),
    )


def _with_conflicts(mapped: list[MappedOperation]) -> Iterator[MappedOperation]:
    by_name: dict[str, list[OperationKey]] = {}
    for m in mapped:
        if m.tool is not None:
            by_name.setdefault(m.tool.name, []).append(m.key)
    for m in mapped:
        if m.tool is None:
            yield m
            continue
        others = tuple(k for k in by_name[m.tool.name] if k != m.key)
        yield MappedOperation(
            m.key,
            m.tool,
            m.plan,
            m.unsupported,
            m.dropped,
            m.selected,
            m.name_problem,
            others,
            m.notes,
        )


def _tool_name(
    key: OperationKey, operation: dict[str, Any], override: Override | None
) -> tuple[str, str | None]:
    derived = _normalize(operation.get("operationId")) or _fallback_name(key)
    if override is None or override.name is None:
        return derived, None
    if _TOOL_NAME.fullmatch(override.name):
        return override.name, None
    return derived, (
        f"override name {override.name!r} is not a valid MCP tool name "
        "(1-128 characters of A-Z a-z 0-9 _ - .)"
    )


def _normalize(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return _NOT_NAME_CHARS.sub("_", raw).strip("_")[:_MAX_NAME].strip("_")


def _fallback_name(key: OperationKey) -> str:
    segments = [_normalize(segment) for segment in key.path.split("/")]
    return "_".join([key.method.lower(), *(s for s in segments if s)])[:_MAX_NAME]


def _description(
    key: OperationKey, operation: dict[str, Any], override: Override | None, limit: int
) -> str:
    if override is not None and override.description is not None:
        text = override.description
    else:
        parts = [
            operation[field].strip()
            for field in ("summary", "description")
            if isinstance(operation.get(field), str) and operation[field].strip()
        ]
        text = "\n\n".join(parts) or str(key)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _output_schema(
    document: dict[str, Any], dialect: str, operation: dict[str, Any]
) -> tuple[dict[str, Any] | None, Literal["object", "wrapped", "none"]]:
    """From the lowest 2xx response, when it has a JSON body; wrapped as `result`
    when that is not an object, since MCP's structured output must be one."""
    responses = operation.get("responses", {})
    explicit = sorted(c for c in responses if len(c) == 3 and c.isdigit() and c.startswith("2"))
    codes = explicit + [c for c in ("2XX", "2xx") if c in responses]
    if not codes:
        return None, "none"
    schema = resolve_media_schema(document, responses[codes[0]])
    if schema is None:
        return None, "none"
    bundle = SchemaBundler(document, dialect)
    resolved = deref(document, schema)
    kind: Literal["object", "wrapped"]
    if _is_object_root(document, resolved):
        root = bundle.convert(resolved)
        root = {"type": "object", **root} if "type" not in root else root
        kind = "object"
    else:
        root = {
            "type": "object",
            "properties": {"result": bundle.convert(schema)},
            "required": ["result"],
        }
        kind = "wrapped"
    return copy.deepcopy(bundle.finish(root)), kind


def _is_object_root(document: dict[str, Any], schema: Any) -> bool:
    if not isinstance(schema, dict) or schema.get("nullable") is True:
        return False
    kind = schema.get("type")
    if kind is not None:
        return kind == "object"
    if "properties" in schema:
        return True
    members = schema.get("allOf")
    return (
        isinstance(members, list)
        and bool(members)
        and all(_is_object_root(document, deref(document, m)) for m in members)
    )
