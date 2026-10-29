"""Bytes to a plain-JSON document, with the size of the result bounded.

A spec arrives as JSON or YAML and is stored as JSON (ADR 0011 §2), so anything that
cannot round-trip through JSON is refused here rather than discovered later. YAML
needs the most care:

- **Keys keep their source spelling.** An unquoted ``200:`` is an int to a YAML
  parser and ``on:`` is a boolean, but in an OpenAPI document they are a status code
  and a property name.
- **YAML 1.1's extra scalars are off.** Unquoted dates stay strings (``version:
  2024-01-01``), and only ``true``/``false`` are booleans -- ``yes``, ``no``, ``on``
  and ``off`` are the enum values they look like.
- **Aliases are bounded.** Parsing shares an aliased object rather than copying it, so
  a few hundred bytes can describe a billion strings that only expand when walked (or
  stored). The walk below counts every *use* and stops at a budget.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, ScalarNode

from mcp_hub.openapi.errors import SpecError
from mcp_hub.openapi.refs import encode_pointer

# Values in the document once aliases are expanded. Large real specs are in the low
# hundreds of thousands; the budget is what stops an alias bomb, so it sits well above
# that and far below anything that would exhaust memory.
MAX_NODES = 2_000_000
# Real specs nest a few dozen levels. Deeper than this is an attack or a bug, and
# either way the recursive code downstream (validator, schema conversion) would fail
# on it less helpfully than this does.
MAX_DEPTH = 128

_UTF8_BOM = b"\xef\xbb\xbf"
_BOOL_TAG = "tag:yaml.org,2002:bool"
_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
_MERGE_TAG = "tag:yaml.org,2002:merge"

_BaseLoader: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _SpecLoader(_BaseLoader):  # type: ignore[misc]
    """`yaml.safe_load` with the spec-specific rules in the module docstring."""

    yaml_implicit_resolvers = {
        first: [(tag, rx) for tag, rx in resolvers if tag not in (_BOOL_TAG, _TIMESTAMP_TAG)]
        for first, resolvers in _BaseLoader.yaml_implicit_resolvers.items()
    }

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[str, Any]:
        if not isinstance(node, MappingNode):
            raise ConstructorError(None, None, "expected a mapping", node.start_mark)
        seen: set[str] = set()
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                continue  # `<<: *base` keys are overridden by design, not duplicated
            key = _scalar_key(key_node)
            if key in seen:
                raise ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        self.flatten_mapping(node)
        # Merged pairs come first, so the mapping's own keys win, as YAML specifies.
        return {
            _scalar_key(key_node): self.construct_object(value_node, deep=deep)
            for key_node, value_node in node.value
        }


_SpecLoader.add_implicit_resolver(
    _BOOL_TAG, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def _scalar_key(key_node: Any) -> str:
    if not isinstance(key_node, ScalarNode):
        raise ConstructorError(
            None, None, "mapping keys must be plain strings", key_node.start_mark
        )
    return str(key_node.value)


def parse_document(data: bytes) -> dict[str, Any]:
    """Parse JSON or YAML into a bounded, JSON-only mapping, or raise `SpecError`."""
    text = _decode(data)
    if not text.strip():
        raise SpecError("Spec is empty")
    document = _parse_json(text) if text.lstrip().startswith("{") else _parse_yaml(text)
    if not isinstance(document, dict):
        raise SpecError("Spec must be an object (a mapping) at the top level")
    _check_plain_json(document)
    return document


def _decode(data: bytes) -> str:
    if data.startswith(_UTF8_BOM):
        data = data[len(_UTF8_BOM) :]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpecError(f"Spec is not valid UTF-8 (byte {exc.start})") from exc


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SpecError(f"Spec has a duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise SpecError(f"Spec contains {name}, which is not a JSON number")


def _parse_json(text: str) -> Any:
    try:
        return json.loads(
            text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
        )
    except json.JSONDecodeError as exc:
        raise SpecError(
            f"Spec is not valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except RecursionError as exc:
        raise SpecError(f"Spec is nested too deeply (more than {MAX_DEPTH} levels)") from exc


def _parse_yaml(text: str) -> Any:
    try:
        return yaml.load(text, Loader=_SpecLoader)  # noqa: S506 -- a SafeLoader subclass
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = exc.problem or exc.context or "invalid YAML"
        raise SpecError(f"Spec is not valid YAML: {problem}{where}") from exc
    except yaml.YAMLError as exc:
        raise SpecError(f"Spec is not valid YAML: {exc}") from exc
    except RecursionError as exc:
        raise SpecError(f"Spec is nested too deeply (more than {MAX_DEPTH} levels)") from exc


def _check_plain_json(document: dict[str, Any]) -> None:
    """Refuse what JSON cannot hold, and anything too large or deep once expanded.

    Sizes count every *use* of a shared (aliased) object, because that is what the
    stored JSON and every later walk will see. They are memoized per object, so the
    cost is in distinct objects: an alias bomb describing 10**9 values is refused
    after looking at a few dozen, rather than after walking the budget.
    """
    # id(container) -> (values in its expanded subtree, height of that subtree)
    expanded: dict[int, tuple[int, int]] = {}
    stack: list[tuple[Any, tuple[Any, ...], bool]] = [(document, (), False)]
    while stack:
        value, path, children_done = stack.pop()
        if not isinstance(value, (dict, list)):
            _check_scalar(value, path)
            continue
        if id(value) in expanded:
            continue
        # Checked on the way down too: a recursive YAML structure (`&a [*a]`) would
        # otherwise never finish, since its children are never done.
        if len(path) > MAX_DEPTH:
            raise _too_deep(path)
        items = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
        if not children_done:
            stack.append((value, path, True))
            stack.extend((child, (*path, key), False) for key, child in items)
            continue
        size, height = 1, 0
        for _, child in items:
            child_size, child_height = (
                expanded[id(child)] if isinstance(child, (dict, list)) else (1, -1)
            )
            size += child_size
            height = max(height, child_height + 1)
        if size > MAX_NODES:
            raise SpecError(
                f"Spec is too large once expanded: more than {MAX_NODES} values "
                "(each use of a YAML alias counts)",
                pointer=encode_pointer(path),
            )
        if len(path) + height > MAX_DEPTH:
            raise _too_deep(path)
        expanded[id(value)] = (size, height)


def _too_deep(path: tuple[Any, ...]) -> SpecError:
    return SpecError(
        f"Spec is nested too deeply (more than {MAX_DEPTH} levels)",
        pointer=encode_pointer(path),
    )


def _check_scalar(value: Any, path: tuple[Any, ...]) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise SpecError(
            f"Spec contains {value}, which is not a JSON number",
            pointer=encode_pointer(path),
        )
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise SpecError(
            f"Spec contains a {type(value).__name__} value, which JSON cannot represent",
            pointer=encode_pointer(path),
        )
