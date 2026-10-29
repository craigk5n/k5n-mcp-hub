"""An operation's parameters and body as tool arguments (Story 13.3, ADR 0011 §6).

Parameters keep their names. An object body's properties are merged in beside them;
a body property that collides with a parameter becomes ``body_<name>``, and if that
collides too the operation is unsupported. A body that cannot be flattened without
changing what the API accepts (arrays, primitives, ``oneOf``/``anyOf``, explicit
``additionalProperties``) is passed whole as one ``body`` argument.

Only what Story 13.5 commits to serializing is supported: ``form`` query parameters
with primitive or array values, ``simple`` path and header parameters, and JSON or
form-urlencoded bodies. Everything else marks the operation unsupported, with a
reason the preview shows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from mcp_hub.openapi.model import BodyPlan, ParameterPlan
from mcp_hub.openapi.schema_convert import SchemaBundler, deref

FORM_MEDIA = "application/x-www-form-urlencoded"
# Never taken from tool arguments. Credentials come only from the server's configured
# auth, transport headers belong to the hub's HTTP client, and forwarding headers
# would let a model claim to be some other client (ADR 0011 §6).
DROPPED_HEADERS = frozenset(
    name.lower()
    for name in (
        "Authorization",
        "Proxy-Authorization",
        "Cookie",
        "Host",
        "Content-Length",
        "Content-Type",
        "Transfer-Encoding",
        "Connection",
        "Keep-Alive",
        "TE",
        "Trailer",
        "Upgrade",
        "Proxy-Connection",
        "Forwarded",
        # Reverse-proxy routing and method-override headers: set by a model, they can
        # turn a call to one operation into a call to another.
        "X-Original-URL",
        "X-Rewrite-URL",
        "X-HTTP-Method-Override",
        "X-HTTP-Method",
        "X-Method-Override",
        "X-Real-IP",
        "Via",
        "Origin",
    )
)
_DEFAULT_STYLE = {"query": "form", "path": "simple", "header": "simple"}
_PATH_VARIABLE = re.compile(r"\{([^}]+)\}")
# allOf members can themselves be allOf; real specs nest a few levels.
_MAX_ALL_OF_DEPTH = 32


@dataclass
class Arguments:
    """Accumulates one operation's arguments. `unsupported` non-empty means the
    operation cannot become a tool, and the rest is incomplete."""

    properties: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    parameters: list[ParameterPlan] = field(default_factory=list)
    body: BodyPlan | None = None
    unsupported: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def is_json_media(media: str) -> bool:
    base = media.split(";", 1)[0].strip().lower()
    return base == "application/json" or (
        base.startswith("application/") and base.endswith("+json")
    )


def operation_arguments(
    document: dict[str, Any],
    bundler: SchemaBundler,
    path: str,
    path_item: dict[str, Any],
    operation: dict[str, Any],
) -> Arguments:
    args = Arguments()
    for param in _merged_parameters(document, path_item, operation):
        _add_parameter(document, bundler, args, param)
    if not path.startswith("/"):
        # Specs that load always start paths with "/"; anything else, appended to a
        # base URL with no path, could change the host the request goes to.
        args.unsupported.append(f"path {path!r} does not start with '/'")
    if any(segment in (".", "..") for segment in path.split("/")):
        # Appended to the base URL, a dot segment would climb out of its path.
        args.unsupported.append(f"path {path!r} contains a '.' or '..' segment")
    if "%" in path or "\\" in path:
        # Percent-encoding (`%2e%2e`, `%2f`) or a backslash in the spec's literal
        # path text is a dot segment or separator to any upstream that decodes it.
        args.unsupported.append(f"path {path!r} contains percent-encoding or a backslash")
    declared = {p.name for p in args.parameters if p.location == "path"}
    for variable in _PATH_VARIABLE.findall(path):
        if variable not in declared:
            args.unsupported.append(f"path variable {{{variable}}} has no declared path parameter")
    if "requestBody" in operation:
        _add_body(document, bundler, args, operation["requestBody"])
    return args


def _merged_parameters(
    document: dict[str, Any], path_item: dict[str, Any], operation: dict[str, Any]
) -> list[dict[str, Any]]:
    """Path-level first; an operation parameter with the same (name, in) replaces it."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for param in [*path_item.get("parameters", []), *operation.get("parameters", [])]:
        resolved = deref(document, param)
        merged[(resolved["name"], resolved["in"])] = resolved
    return list(merged.values())


def _add_parameter(
    document: dict[str, Any], bundler: SchemaBundler, args: Arguments, param: dict[str, Any]
) -> None:
    name, location = param["name"], param["in"]
    if location == "cookie":
        args.dropped.append(f"cookie parameter '{name}' (the hub never sends cookies)")
        return
    if location not in _DEFAULT_STYLE:
        # e.g. OpenAPI 3.2's `in: querystring`: the whole query string as one value.
        args.unsupported.append(f"parameter '{name}' is in: {location}, which is not supported")
        return
    if location == "header" and (
        name.lower() in DROPPED_HEADERS or name.lower().startswith("x-forwarded-")
    ):
        args.dropped.append(
            f"header '{name}' (credentials and transport headers come from the hub, "
            "never from tool arguments)"
        )
        return
    problem = _parameter_problem(document, param)
    if problem:
        args.unsupported.append(problem)
        return
    if name in args.properties:
        args.unsupported.append(f"two parameters are both named '{name}'")
        return
    style = param.get("style", _DEFAULT_STYLE[location])
    explode = param.get("explode", style == "form")
    schema = bundler.convert(param["schema"])
    if isinstance(schema, dict) and "description" in param and "description" not in schema:
        schema = {**schema, "description": param["description"]}
    args.properties[name] = schema
    if location == "path" or param.get("required"):
        args.required.append(name)
    args.parameters.append(ParameterPlan(name, name, location, style, explode))


def _parameter_problem(document: dict[str, Any], param: dict[str, Any]) -> str | None:
    name, location = param["name"], param["in"]
    where = f"{location} parameter '{name}'"
    if "content" in param or "schema" not in param:
        return f"{where} uses content serialization, which is not supported"
    style = param.get("style", _DEFAULT_STYLE[location])
    if style != _DEFAULT_STYLE[location]:
        return f"{where} uses style '{style}', which is not supported"
    value = deref(document, param["schema"])
    if not isinstance(value, dict):
        return None
    if _is_object(value):
        return f"{where} has an object value, which is not supported"
    if value.get("type") == "array" and _is_array(deref(document, value.get("items"))):
        return f"{where} is an array of arrays, which is not supported"
    return None


def _is_object(schema: dict[str, Any]) -> bool:
    kind = schema.get("type")
    return kind == "object" or (kind is None and "properties" in schema)


def _is_array(schema: Any) -> bool:
    return isinstance(schema, dict) and schema.get("type") == "array"


def _add_body(
    document: dict[str, Any], bundler: SchemaBundler, args: Arguments, request_body: Any
) -> None:
    body = deref(document, request_body)
    content = body.get("content", {})
    media = next((m for m in content if is_json_media(m)), None) or (
        FORM_MEDIA if FORM_MEDIA in content else None
    )
    if media is None:
        offered = ", ".join(content) or "none"
        args.unsupported.append(
            f"request body type(s) {offered} are not supported (only JSON and "
            "application/x-www-form-urlencoded)"
        )
        return
    if "schema" not in content[media]:
        args.unsupported.append(f"the {media} request body has no schema")
        return
    schema = content[media]["schema"]
    required = bool(body.get("required"))
    fields = object_fields(document, schema)
    if fields is None:
        if media == FORM_MEDIA:
            args.unsupported.append("a form-urlencoded request body must be an object")
            return
        _add_whole_body(bundler, args, schema, media, required)
    else:
        _add_merged_body(bundler, args, fields, media, required)


def _add_whole_body(
    bundler: SchemaBundler, args: Arguments, schema: Any, media: str, required: bool
) -> None:
    argument = _body_argument(args, "body")
    if argument is None:
        return
    args.properties[argument] = bundler.convert(schema)
    if required:
        args.required.append(argument)
    args.body = BodyPlan(media, "whole", ((argument, ""),), required)


def _add_merged_body(
    bundler: SchemaBundler,
    args: Arguments,
    fields: tuple[dict[str, Any], list[str]],
    media: str,
    required: bool,
) -> None:
    properties, body_required = fields
    renamed: list[tuple[str, str]] = []
    for prop in properties:
        argument = _body_argument(args, prop, properties)
        if argument is None:
            return
        renamed.append((argument, prop))
    for argument, prop in renamed:
        args.properties[argument] = bundler.convert(properties[prop])
    if required:
        names = {prop: argument for argument, prop in renamed}
        args.required.extend(names[prop] for prop in body_required if prop in names)
    args.body = BodyPlan(media, "merged", tuple(renamed), required)


def _body_argument(
    args: Arguments, name: str, body_properties: dict[str, Any] | None = None
) -> str | None:
    """`name`, or ``body_<name>`` when a parameter already has it; None (and the
    operation unsupported) when the renamed one is taken as well, by a parameter or by
    another body property."""
    if name not in args.properties:
        return name
    renamed = f"body_{name}"
    if renamed in args.properties or (body_properties is not None and renamed in body_properties):
        args.unsupported.append(
            f"body property '{name}' collides with a parameter, and its renamed form "
            f"'{renamed}' is taken too"
        )
        return None
    return renamed


def object_fields(
    document: dict[str, Any], schema: Any, depth: int = 0
) -> tuple[dict[str, Any], list[str]] | None:
    """(properties, required) when `schema` is an object that can be flattened into
    arguments without changing what the API accepts; None when it must go whole.

    Absent `additionalProperties` counts as closed: that is the JSON Schema default for
    nearly every body, and treating it as open would mean nothing ever merges.
    """
    resolved = deref(document, schema)
    if not isinstance(resolved, dict) or depth > _MAX_ALL_OF_DEPTH:
        return None
    if any(key in resolved for key in ("oneOf", "anyOf", "not", "if", "patternProperties")):
        return None
    extra = resolved.get("additionalProperties")
    if extra is True or isinstance(extra, dict):
        return None
    if resolved.get("type") not in (None, "object") or resolved.get("nullable") is True:
        return None
    properties: dict[str, Any] = {}
    required: list[str] = []
    for member in resolved.get("allOf", []):
        fields = object_fields(document, member, depth + 1)
        if fields is None:
            return None
        properties.update(fields[0])
        required.extend(r for r in fields[1] if r not in required)
    if (
        "allOf" not in resolved
        and "properties" not in resolved
        and resolved.get("type") != "object"
    ):
        return None
    properties.update(resolved.get("properties", {}))
    required.extend(r for r in resolved.get("required", []) if r not in required)
    return properties, required


def resolve_media_schema(document: dict[str, Any], response: Any) -> Any:
    """The JSON schema of a response, or None when it has no JSON body."""
    resolved = deref(document, response)
    content = resolved.get("content", {}) if isinstance(resolved, dict) else {}
    media = next((m for m in content if is_json_media(m)), None)
    return content[media].get("schema") if media else None


def streaming_media(document: dict[str, Any], operation: dict[str, Any]) -> list[str]:
    """Reasons an operation streams: OpenAPI 3.2's `itemSchema` describes each item of a
    sequential body (SSE, JSON Lines), and one MCP tool result cannot carry a stream."""
    reasons: list[str] = []
    if "requestBody" in operation:
        body = deref(document, operation["requestBody"])
        for media, value in (body.get("content", {}) if isinstance(body, dict) else {}).items():
            if isinstance(value, dict) and "itemSchema" in value:
                reasons.append(
                    f"the {media} request body streams (itemSchema), which is not supported"
                )
    for code, response in operation.get("responses", {}).items():
        resolved = deref(document, response)
        content = resolved.get("content", {}) if isinstance(resolved, dict) else {}
        for media, value in content.items():
            if isinstance(value, dict) and "itemSchema" in value:
                reasons.append(
                    f"the {code} {media} response streams (itemSchema), which is not supported"
                )
    return reasons
