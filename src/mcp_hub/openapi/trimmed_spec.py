"""The spec an OpenAPI server serves as its one MCP resource (ADR 0011 §9).

Trimmed to what a caller can use, and built from **allowlists**, not by deleting
known-bad keys: a spec is untrusted text, and whatever this does not name is left
out.

- Only the selected operations, each holding only the fields mapping reads
  (`_OPERATION_FIELDS`). Operation `servers`, `security`, `callbacks` and every
  `x-*` extension are gone with everything else not listed.
- Responses keep their description, content, headers and schema. `links`, which name
  other operations, are dropped.
- `info` is reduced to `title`, `version` and `summary`. Its description and contact
  details often name intranet URLs or people.
- Only the components (3.x) or definitions/parameters/responses (2.0) the kept
  operations reach through `$ref`, transitively. A `$ref` that would point anywhere
  else -- at a disabled path, say -- is removed rather than left naming its target.
- `x-*` keys are stripped everywhere except where keys are names (`properties`,
  `patternProperties`).
- Only the tags in use. No `servers`, `host`, `basePath`, `schemes`, `webhooks`,
  `externalDocs` or security definitions.

Pure, and the input is never modified.
"""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import unquote

from mcp_hub.openapi.model import OperationKey
from mcp_hub.openapi.refs import decode_pointer, encode_pointer, resolve_local

_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_OPERATION_FIELDS = (
    "operationId",
    "summary",
    "description",
    "tags",
    "deprecated",
    "parameters",
    "requestBody",
    "responses",
    "consumes",  # 2.0
    "produces",  # 2.0
)
_RESPONSE_FIELDS = ("description", "content", "headers", "schema", "examples", "$ref")
_INFO_FIELDS = ("title", "version", "summary")
# Swagger 2.0's top-level containers, kept only where something kept refers to them.
# (3.x keeps the same rule for every `components` kind.)
_CONTAINERS_20 = ("definitions", "parameters", "responses")
# Maps whose keys are names chosen by the spec author, not keywords or extensions.
_NAME_MAPS = ("properties", "patternProperties")


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
            method: _operation(item[method])
            for method in _METHODS
            if isinstance(item.get(method), dict) and OperationKey(method, path) in selection
        }
        if kept:
            if "parameters" in item:
                kept = {"parameters": item["parameters"], **kept}
            paths[path] = kept

    out: dict[str, Any] = {key: document[key] for key in ("openapi", "swagger") if key in document}
    info = document.get("info", {})
    out["info"] = {key: info[key] for key in _INFO_FIELDS if key in info}
    out["paths"] = paths
    out.update(_reachable_containers(document, dialect, paths))
    used_tags = {
        tag for item in paths.values() for op in _operations(item) for tag in op.get("tags", [])
    }
    tags = [
        {key: tag[key] for key in ("name", "description") if key in tag}
        for tag in document.get("tags", [])
        if isinstance(tag, dict) and tag.get("name") in used_tags
    ]
    if tags:
        out["tags"] = tags
    cleaned = _strip_extensions(copy.deepcopy(out))
    _drop_dangling_refs(cleaned)
    return cleaned


def _operation(operation: dict[str, Any]) -> dict[str, Any]:
    kept = {key: operation[key] for key in _OPERATION_FIELDS if key in operation}
    if isinstance(kept.get("responses"), dict):
        kept["responses"] = {
            code: _response(response)
            for code, response in kept["responses"].items()
            if not code.startswith("x-")
        }
    return kept


def _response(response: Any) -> Any:
    if not isinstance(response, dict):
        return response
    return {key: response[key] for key in _RESPONSE_FIELDS if key in response}


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
                target = resolve_local(document, "#" + encode_pointer(entry))
                # Responses reached through a ref are trimmed like inline ones.
                stack.append(_response(target) if entry[-2] == "responses" else target)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)

    out: dict[str, Any] = {}
    if dialect == "2.0":
        for container in _CONTAINERS_20:
            entries = {
                name: _response(value) if container == "responses" else value
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
            name: _response(value) if kind == "responses" else value
            for name, value in entries.items()
            if ("components", kind, name) in reached
        }
        if kept:
            components[kind] = kept
    if components:
        out["components"] = components
    return out


def _strip_extensions(node: Any, *, names: bool = False) -> Any:
    """Drop `x-*` keys, except in maps whose keys are names (`names=True`)."""
    if isinstance(node, list):
        return [_strip_extensions(item) for item in node]
    if not isinstance(node, dict):
        return node
    return {
        key: _strip_extensions(value, names=key in _NAME_MAPS and not names)
        for key, value in node.items()
        if names or not key.startswith("x-")
    }


def _drop_dangling_refs(document: dict[str, Any]) -> None:
    """Remove any `$ref` that does not resolve inside the trimmed document. It would
    otherwise still name what it pointed at -- a disabled path, a dropped
    component."""
    stack: list[Any] = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not _resolves(document, ref):
                del node["$ref"]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _resolves(document: dict[str, Any], ref: str) -> bool:
    if not ref.startswith("#/"):
        return False
    try:
        resolve_local(document, ref)
    except (KeyError, IndexError, ValueError, TypeError):
        return False
    return True
