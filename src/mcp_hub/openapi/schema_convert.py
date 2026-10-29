"""OpenAPI schemas to self-contained JSON Schema 2020-12 for MCP tools (Story 13.3).

A tool's `inputSchema`/`outputSchema` is read by MCP clients that know nothing of
the spec, so `#/components/...` refs mean nothing to them. Each referenced schema is
**bundled** under the root's `$defs` and the ref rewritten to `#/$defs/<Name>`. Refs
stay refs, so a recursive type terminates, and the hub's existing display resolver
(`mcp.schema_refs`) reads these tools as it reads any other.

OpenAPI 3.0's schema dialect is converted on the way: `nullable` becomes a `"null"`
type, the boolean `exclusiveMinimum`/`exclusiveMaximum` become numbers, `example`
becomes `examples`, and `$ref` siblings are dropped (3.0 ignores them). 3.1 schemas
already are 2020-12. Both lose OpenAPI-only keywords (`discriminator`, `xml`,
`externalDocs`, `x-*`), which mean nothing to a JSON Schema validator.
"""

from __future__ import annotations

import re
from typing import Any

from mcp_hub.openapi.refs import resolve_local

_OPENAPI_ONLY = frozenset({"discriminator", "xml", "externalDocs", "nullable", "example"})
_ONE_SCHEMA = (
    "items",
    "additionalProperties",
    "not",
    "if",
    "then",
    "else",
    "contains",
    "propertyNames",
    "unevaluatedProperties",
    "unevaluatedItems",
    "additionalItems",
)
_SCHEMA_MAPS = ("properties", "patternProperties", "dependentSchemas", "$defs")
_SCHEMA_LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")
_COMPONENT_SCHEMA = "#/components/schemas/"
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def deref(document: dict[str, Any], schema: Any) -> Any:
    """Follow a chain of `$ref`s to the schema it ends at. Terminates because pure
    ref cycles are refused at load (`refs.check_refs`)."""
    while isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        schema = resolve_local(document, schema["$ref"])
    return schema


class SchemaBundler:
    """Converts schemas for one tool root, collecting what they reference."""

    def __init__(self, document: dict[str, Any], dialect: str) -> None:
        self._document = document
        self._is_30 = dialect == "3.0"
        self._names: dict[str, str] = {}
        self._pending: list[tuple[str, str]] = []
        self._defs: dict[str, Any] = {}

    def convert(self, schema: Any) -> Any:
        if not isinstance(schema, dict):
            return schema  # 3.1 boolean schemas
        if isinstance(schema.get("$ref"), str):
            ref = {"$ref": "#/$defs/" + self._def_name(schema["$ref"])}
            if self._is_30:
                return ref  # 3.0: "properties adjacent to $ref are ignored"
            return {**ref, **self._convert_keywords(schema, skip={"$ref"})}
        return self._convert_keywords(schema)

    def finish(self, root: dict[str, Any]) -> dict[str, Any]:
        """`root` with every referenced schema bundled under `$defs`."""
        while self._pending:
            name, ref = self._pending.pop()
            self._defs[name] = self.convert(resolve_local(self._document, ref))
        return {**root, "$defs": dict(self._defs)} if self._defs else root

    def _convert_keywords(
        self, schema: dict[str, Any], skip: frozenset[str] | set[str] = frozenset()
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key in skip or key in _OPENAPI_ONLY or key.startswith("x-"):
                continue
            if key in _ONE_SCHEMA:
                out[key] = self.convert(value)
            elif key in _SCHEMA_MAPS and isinstance(value, dict):
                out[key] = {name: self.convert(sub) for name, sub in value.items()}
            elif key in _SCHEMA_LISTS and isinstance(value, list):
                out[key] = [self.convert(sub) for sub in value]
            else:
                out[key] = value
        if "example" in schema and "examples" not in out:
            out["examples"] = [schema["example"]]
        if self._is_30:
            out = _convert_30_keywords(schema, out)
        return out

    def _def_name(self, ref: str) -> str:
        if ref in self._names:
            return self._names[ref]
        if ref.startswith(_COMPONENT_SCHEMA) and "/" not in ref[len(_COMPONENT_SCHEMA) :]:
            base = ref[len(_COMPONENT_SCHEMA) :]
        else:
            base = ref.lstrip("#/")
        base = _UNSAFE_NAME.sub("_", base).strip("_") or "schema"
        name, n = base, 1
        while name in self._names.values():
            n += 1
            name = f"{base}_{n}"
        self._names[ref] = name
        self._pending.append((name, ref))
        return name


def _convert_30_keywords(schema: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    for exclusive, bound in (("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum")):
        value = schema.get(exclusive)
        if isinstance(value, bool):
            out.pop(exclusive, None)
            if value and bound in schema:
                out[exclusive] = schema[bound]
                out.pop(bound, None)
    if schema.get("nullable") is True:
        kind = out.get("type")
        if isinstance(kind, str):
            out["type"] = [kind, "null"]
            if isinstance(out.get("enum"), list) and None not in out["enum"]:
                out["enum"] = [*out["enum"], None]
        elif kind is None:
            out = {"anyOf": [out, {"type": "null"}]}
    return out
