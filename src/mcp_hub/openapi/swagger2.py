"""Swagger 2.0 to the OpenAPI 3.0 shape that tool mapping consumes (Story 13.2).

Scoped to what mapping needs (ADR 0011 §11), not a general-purpose converter:

- Path-level parameters and global ``#/parameters/...`` refs are **inlined** into each
  operation, so mapping only ever reads an operation's own parameter list. ``body``
  and ``formData`` parameters become a ``requestBody``.
- ``consumes``/``produces`` become media types; an operation's own list wins.
- ``definitions`` and global ``responses`` move under ``components`` and every ref to
  them is rewritten.
- ``host``/``basePath``/``schemes`` become a ``servers[]`` *suggestion*. The admin
  sets the real base URL (ADR 0011 §4).

What cannot be represented does not fail the import. The operation gets a reason in
``x-mcp-hub-unsupported`` (`UNSUPPORTED`), stays visible in the preview, and cannot
be enabled (ADR 0011 §6). The output is still a valid 3.0 document.

The input must have passed `loader.load_spec`: refs are assumed local and resolvable.
It is never modified, and the output shares no objects with it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from mcp_hub.openapi.refs import resolve_local

UNSUPPORTED = "x-mcp-hub-unsupported"
OPENAPI_VERSION = "3.0.3"

_METHODS = ("get", "put", "post", "delete", "options", "head", "patch")
_JSON = "application/json"
_FORM = "application/x-www-form-urlencoded"
_MULTIPART = "multipart/form-data"
# Keys of a 2.0 non-body parameter, items or header object that describe its value
# and so move into a 3.0 `schema`. `items` is handled separately (it recurses).
_VALUE_KEYWORDS = (
    "type",
    "format",
    "default",
    "maximum",
    "exclusiveMaximum",
    "minimum",
    "exclusiveMinimum",
    "maxLength",
    "minLength",
    "pattern",
    "maxItems",
    "minItems",
    "uniqueItems",
    "enum",
    "multipleOf",
)
_PARAMETER_FIELDS = ("name", "in", "description", "required", "allowEmptyValue")
_OPERATION_FIELDS_DROPPED = ("parameters", "responses", "consumes", "produces", "schemes")
_REF_PREFIXES = (
    ("#/definitions/", "#/components/schemas/"),
    ("#/responses/", "#/components/responses/"),
)
# (collectionFormat, location) -> (style, explode). A pair that is absent has no 3.0
# equivalent: tsv anywhere, and the query-only formats outside query and form bodies.
_STYLES = {
    ("csv", "query"): ("form", False),
    ("csv", "path"): ("simple", False),
    ("csv", "header"): ("simple", False),
    ("csv", "formData"): ("form", False),
    ("multi", "query"): ("form", True),
    ("multi", "formData"): ("form", True),
    ("ssv", "query"): ("spaceDelimited", False),
    ("ssv", "formData"): ("spaceDelimited", False),
    ("pipes", "query"): ("pipeDelimited", False),
    ("pipes", "formData"): ("pipeDelimited", False),
}
_OAUTH2_FLOWS = {
    "implicit": ("implicit", ("authorizationUrl",)),
    "password": ("password", ("tokenUrl",)),
    "application": ("clientCredentials", ("tokenUrl",)),
    "accessCode": ("authorizationCode", ("authorizationUrl", "tokenUrl")),
}


@dataclass(frozen=True)
class _Context:
    document: dict[str, Any]
    consumes: tuple[str, ...]
    produces: tuple[str, ...]

    def resolve(self, ref: str) -> Any:
        return copy.deepcopy(resolve_local(self.document, ref))

    def parameter(self, param: dict[str, Any]) -> dict[str, Any]:
        return self.resolve(param["$ref"]) if "$ref" in param else param


def swagger2_to_openapi3(document: dict[str, Any]) -> dict[str, Any]:
    """Convert a validated Swagger 2.0 document. Raises ValueError for any other input."""
    if document.get("swagger") != "2.0":
        raise ValueError("swagger2_to_openapi3 expects a Swagger 2.0 document")
    source = copy.deepcopy(document)
    ctx = _Context(
        document=source,
        consumes=tuple(source.get("consumes", ())),
        produces=tuple(source.get("produces", ())),
    )
    out: dict[str, Any] = {"openapi": OPENAPI_VERSION, "info": source["info"]}
    servers = _servers(source)
    if servers:
        out["servers"] = servers
    out.update({key: source[key] for key in ("tags", "security", "externalDocs") if key in source})
    out["paths"] = {
        path: item if path.startswith("x-") else _path_item(ctx, item)
        for path, item in source["paths"].items()
    }
    components = _components(ctx)
    if components:
        out["components"] = components
    out.update(_extensions(source))
    return out


def _extensions(obj: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in obj.items() if key.startswith("x-")}


def _servers(source: dict[str, Any]) -> list[dict[str, str]]:
    host = source.get("host")
    base = source.get("basePath", "").rstrip("/")
    if host:
        if "schemes" not in source:
            # The spec's own scheme applies, which only whoever fetched it knows.
            return [{"url": f"//{host}{base}"}]
        schemes = [s for s in source["schemes"] if s in ("http", "https")]
        return [{"url": f"{scheme}://{host}{base}"} for scheme in schemes]
    if "basePath" in source:
        return [{"url": base or "/"}]
    return []


def _path_item(ctx: _Context, item: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in item:
        item = ctx.resolve(item["$ref"])
    shared = [ctx.parameter(p) for p in item.get("parameters", [])]
    out: dict[str, Any] = {}
    for key, value in item.items():
        if key in _METHODS:
            out[key] = _operation(ctx, value, shared)
        elif key.startswith("x-"):
            out[key] = value
    return out


def _merge_parameters(
    shared: list[dict[str, Any]], own: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Path-level first; an operation parameter with the same (name, in) replaces it."""
    merged = list(shared)
    position = {(p["name"], p["in"]): i for i, p in enumerate(merged)}
    for param in own:
        key = (param["name"], param["in"])
        if key in position:
            merged[position[key]] = param
        else:
            position[key] = len(merged)
            merged.append(param)
    return merged


def _operation(ctx: _Context, op: dict[str, Any], shared: list[dict[str, Any]]) -> dict[str, Any]:
    params = _merge_parameters(
        [copy.deepcopy(p) for p in shared], [ctx.parameter(p) for p in op.get("parameters", [])]
    )
    consumes = tuple(op["consumes"]) if "consumes" in op else ctx.consumes
    produces = tuple(op["produces"]) if "produces" in op else ctx.produces
    out = {k: v for k, v in op.items() if k not in _OPERATION_FIELDS_DROPPED}
    reasons: list[str] = []

    regular = []
    for param in params:
        if param["in"] not in ("body", "formData"):
            converted, problems = _parameter(param)
            regular.append(converted)
            reasons.extend(problems)
    if regular:
        out["parameters"] = regular

    body = [p for p in params if p["in"] == "body"]
    form = [p for p in params if p["in"] == "formData"]
    if body and form:
        reasons.append(
            "operation has both a body parameter and formData parameters, "
            "but a request has only one body"
        )
    if body:
        out["requestBody"] = _body(body[0], consumes)
    elif form:
        out["requestBody"], problems = _form_body(form, consumes)
        reasons.extend(problems)

    out["responses"] = {
        code: response if code.startswith("x-") else _response(response, produces)
        for code, response in op["responses"].items()
    }
    if reasons:
        out[UNSUPPORTED] = reasons
    return out


def _value_schema(obj: dict[str, Any], where: str) -> tuple[dict[str, Any], list[str]]:
    """The schema of a non-body parameter, items or header object."""
    schema = {key: obj[key] for key in _VALUE_KEYWORDS if key in obj}
    reasons: list[str] = []
    if obj.get("type") == "file":
        schema.update(type="string", format="binary")
    if isinstance(obj.get("items"), dict):
        items = obj["items"]
        if items.get("type") == "array":
            reasons.append(
                f"{where} is a nested array (an array of arrays), which is not supported"
            )
        schema["items"], _ = _value_schema(items, where)
    return schema, reasons


def _style(param: dict[str, Any]) -> tuple[tuple[str, bool] | None, str]:
    fmt = param.get("collectionFormat", "csv")
    style = _STYLES.get((fmt, param["in"]))
    reason = (
        f"parameter '{param['name']}' uses collectionFormat '{fmt}' in {param['in']}, "
        "which has no OpenAPI 3 equivalent"
    )
    return style, reason


def _parameter(param: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    out = {key: param[key] for key in _PARAMETER_FIELDS if key in param}
    out.update(_extensions(param))
    out["schema"], reasons = _value_schema(param, f"parameter '{param['name']}'")
    if param.get("type") == "array":
        style, reason = _style(param)
        if style is None:
            reasons.append(reason)
        else:
            out["style"], out["explode"] = style
    return out, reasons


def _body(param: dict[str, Any], consumes: tuple[str, ...]) -> dict[str, Any]:
    schema = _schema(param["schema"])
    out: dict[str, Any] = {key: param[key] for key in ("description",) if key in param}
    if param.get("required"):
        out["required"] = True
    out["content"] = {media: {"schema": copy.deepcopy(schema)} for media in consumes or (_JSON,)}
    out.update(_extensions(param))
    return out


def _form_body(
    params: list[dict[str, Any]], consumes: tuple[str, ...]
) -> tuple[dict[str, Any], list[str]]:
    media = _MULTIPART if _MULTIPART in consumes and _FORM not in consumes else _FORM
    properties: dict[str, Any] = {}
    required: list[str] = []
    encoding: dict[str, Any] = {}
    reasons: list[str] = []
    for param in params:
        name = param["name"]
        schema, problems = _value_schema(param, f"formData parameter '{name}'")
        reasons.extend(problems)
        if param.get("type") == "file":
            reasons.append(
                f"formData parameter '{name}' is a file upload (type: file), which is not supported"
            )
        if "description" in param:
            schema["description"] = param["description"]
        properties[name] = schema
        if param.get("required"):
            required.append(name)
        if param.get("type") == "array":
            style, reason = _style(param)
            if style is None:
                reasons.append(reason)
            else:
                encoding[name] = {"style": style[0], "explode": style[1]}
    schema_out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema_out["required"] = required
    media_out: dict[str, Any] = {"schema": schema_out}
    if encoding:
        media_out["encoding"] = encoding
    out: dict[str, Any] = {"required": True} if required else {}
    out["content"] = {media: media_out}
    return out, reasons


def _response(response: dict[str, Any], produces: tuple[str, ...]) -> dict[str, Any]:
    if "$ref" in response:
        return {"$ref": _rewrite_ref(response["$ref"])}
    out: dict[str, Any] = {"description": response.get("description", "")}
    if "headers" in response:
        out["headers"] = {name: _header(h) for name, h in response["headers"].items()}
    if "schema" in response:
        schema = _schema(response["schema"])
        examples = response.get("examples", {})
        content: dict[str, Any] = {}
        for media in produces or (_JSON,):
            content[media] = {"schema": copy.deepcopy(schema)}
            if media in examples:
                content[media]["example"] = examples[media]
        out["content"] = content
    out.update(_extensions(response))
    return out


def _header(header: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {key: header[key] for key in ("description",) if key in header}
    out["schema"], _ = _value_schema(header, "header")
    out.update(_extensions(header))
    return out


def _rewrite_ref(ref: str) -> str:
    for old, new in _REF_PREFIXES:
        if ref.startswith(old):
            return new + ref[len(old) :]
    return ref


def _schema(schema: Any) -> Any:
    """A 2.0 schema object as 3.0, walking only schema positions."""
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "$ref" and isinstance(value, str):
            out[key] = _rewrite_ref(value)
        elif key == "x-nullable":
            if value is True:
                out["nullable"] = True
        elif key == "discriminator" and isinstance(value, str):
            out[key] = {"propertyName": value}
        elif key == "properties" and isinstance(value, dict):
            out[key] = {name: _schema(sub) for name, sub in value.items()}
        elif key in ("items", "additionalProperties"):
            out[key] = _schema(value)
        elif key == "allOf" and isinstance(value, list):
            out[key] = [_schema(sub) for sub in value]
        else:
            out[key] = value
    if out.get("type") == "file":
        out.update(type="string", format="binary")
    return out


def _components(ctx: _Context) -> dict[str, Any]:
    source = ctx.document
    components: dict[str, Any] = {}
    if source.get("definitions"):
        components["schemas"] = {n: _schema(s) for n, s in source["definitions"].items()}
    if source.get("responses"):
        components["responses"] = {
            n: _response(r, ctx.produces) for n, r in source["responses"].items()
        }
    if source.get("securityDefinitions"):
        components["securitySchemes"] = {
            n: _security_scheme(s) for n, s in source["securityDefinitions"].items()
        }
    return components


def _security_scheme(scheme: dict[str, Any]) -> dict[str, Any]:
    kind = scheme["type"]
    out: dict[str, Any]
    if kind == "basic":
        out = {"type": "http", "scheme": "basic"}
    elif kind == "apiKey":
        out = {"type": "apiKey", "name": scheme["name"], "in": scheme["in"]}
    else:
        flow_name, urls = _OAUTH2_FLOWS[scheme["flow"]]
        flow = {key: scheme[key] for key in urls}
        flow["scopes"] = scheme.get("scopes", {})
        out = {"type": "oauth2", "flows": {flow_name: flow}}
    if "description" in scheme:
        out["description"] = scheme["description"]
    out.update(_extensions(scheme))
    return out
