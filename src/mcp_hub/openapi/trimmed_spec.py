"""The spec an OpenAPI server serves as its one MCP resource (ADR 0011 §9).

Trimmed to the enabled operations, so a caller learns about what it can call and
nothing else:

- Only selected operations remain, with their path-level parameters.
- Only the components (3.x) or definitions/parameters/responses (2.0) that those
  operations reach, transitively, through `$ref`.
- Only the tags they use.
- No `servers`, `host`, `basePath` or `schemes`: the REST API's own address is the
  admin's business, and callers reach it through the hub.
- No top-level extensions, `webhooks`, `externalDocs` or security definitions.

Pure, and the input is never modified.
"""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import unquote

from mcp_hub.openapi.model import OperationKey
from mcp_hub.openapi.refs import decode_pointer, encode_pointer, resolve_local

_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
# Swagger 2.0's top-level containers, kept only where something kept refers to them.
# (3.x keeps the same rule for every `components` kind.)
_CONTAINERS_20 = ("definitions", "parameters", "responses")
_KEPT_TOP_LEVEL = ("openapi", "swagger", "info", "jsonSchemaDialect")


def trimmed_spec(
    document: dict[str, Any], dialect: str, selection: frozenset[OperationKey]
) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for path, item in document.get("paths", {}).items():
        if path.startswith("x-") or not isinstance(item, dict):
            continue
        if isinstance(item.get("$ref"), str):
            item = resolve_local(document, item["$ref"])
        kept = {
            method: item[method]
            for method in _METHODS
            if isinstance(item.get(method), dict) and OperationKey(method, path) in selection
        }
        if kept:
            if "parameters" in item:
                kept = {"parameters": item["parameters"], **kept}
            paths[path] = kept

    out: dict[str, Any] = {key: document[key] for key in _KEPT_TOP_LEVEL if key in document}
    out["paths"] = paths
    out.update(_reachable_containers(document, dialect, paths))
    used_tags = {
        tag for item in paths.values() for op in _operations(item) for tag in op.get("tags", [])
    }
    tags = [
        tag
        for tag in document.get("tags", [])
        if isinstance(tag, dict) and tag.get("name") in used_tags
    ]
    if tags:
        out["tags"] = tags
    return copy.deepcopy(out)


def _operations(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [item[m] for m in _METHODS if isinstance(item.get(m), dict)]


def _entry(ref: str) -> tuple[str, ...] | None:
    """The container entry a local ref lands in: ``("components", kind, name)`` for
    3.x, ``(container, name)`` for 2.0's top-level containers, else None."""
    if not ref.startswith("#/"):
        return None
    parts = tuple(decode_pointer(unquote(ref[1:])))
    if parts[:1] == ("components",) and len(parts) >= 3:
        return parts[:3]
    if parts[:1] and parts[0] in _CONTAINERS_20 and len(parts) >= 2:
        return parts[:2]
    return None


def _reachable_containers(document: dict[str, Any], dialect: str, roots: Any) -> dict[str, Any]:
    """The container entries `roots` reaches through refs, transitively."""
    reached: set[tuple[str, ...]] = set()
    stack: list[Any] = [roots]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            entry = _entry(ref) if isinstance(ref, str) else None
            if entry is not None and entry not in reached:
                reached.add(entry)
                stack.append(resolve_local(document, "#" + encode_pointer(entry)))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)

    out: dict[str, Any] = {}
    if dialect == "2.0":
        for container in _CONTAINERS_20:
            entries = {
                name: value
                for name, value in document.get(container, {}).items()
                if (container, name) in reached
            }
            if entries:
                out[container] = entries
        return out
    components: dict[str, Any] = {}
    for kind, entries in document.get("components", {}).items():
        if not isinstance(entries, dict):
            continue
        kept = {
            name: value for name, value in entries.items() if ("components", kind, name) in reached
        }
        if kept:
            components[kind] = kept
    if components:
        out["components"] = components
    return out
