"""Argument validation against a tool's `inputSchema`, safe from regex backtracking.

A spec's `pattern` is attacker-chosen and tool arguments come from a model, so the
stock `jsonschema` validator -- which runs `pattern` and `patternProperties` through
Python's backtracking `re` -- can be made to hang a live tool call while holding the
GIL (the Story 13.1 review hit exactly this in the spec validator). Here both
keywords use RE2, which runs in linear time.

RE2 does not support backreferences or lookaround. A pattern it cannot compile is
**not checked**: the upstream API still enforces its own rules, and the preview tells
the admin which patterns those are (`unchecked_patterns`). Refusing every call to such
an operation instead would make a valid tool unusable over a regex feature.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from typing import Any

import re2
from jsonschema import Draft202012Validator, ValidationError, validators

# Distinct patterns kept compiled. A spec has a bounded number of them; this only
# stops a pathological one from growing the cache without limit.
_PATTERN_CACHE = 2048
_MAX_MESSAGE = 300


@functools.lru_cache(maxsize=_PATTERN_CACHE)
def _compile(pattern: str) -> Any | None:
    options = re2.Options()
    options.log_errors = False  # RE2 otherwise writes each failure to stderr
    try:
        return re2.compile(pattern, options)
    except re2.error:
        return None


def _pattern(validator: Any, pattern: str, instance: Any, schema: Any) -> Iterator[ValidationError]:
    if not validator.is_type(instance, "string"):
        return
    compiled = _compile(pattern)
    if compiled is not None and compiled.search(instance) is None:
        yield ValidationError(f"{instance!r} does not match the pattern {pattern!r}")


def _pattern_properties(
    validator: Any, patterns: dict[str, Any], instance: Any, schema: Any
) -> Iterator[ValidationError]:
    if not validator.is_type(instance, "object"):
        return
    for pattern, subschema in patterns.items():
        compiled = _compile(pattern)
        if compiled is None:
            continue
        for key, value in instance.items():
            if compiled.search(key) is not None:
                yield from validator.descend(value, subschema, path=key, schema_path=pattern)


_Validator = validators.extend(
    Draft202012Validator,
    {"pattern": _pattern, "patternProperties": _pattern_properties},
)


def argument_error(schema: dict[str, Any], arguments: Any) -> str | None:
    """Why `arguments` do not satisfy `schema`, or None. The schema must be one the
    hub built (self-contained, local refs only), never one taken raw from a spec."""
    try:
        error = next(iter(sorted(_Validator(schema).iter_errors(arguments), key=_relevance)), None)
    except RecursionError:
        return "the arguments are nested too deeply to validate"
    if error is None:
        return None
    where = "/".join(str(part) for part in error.absolute_path)
    message = (
        error.message if len(error.message) <= _MAX_MESSAGE else error.message[:_MAX_MESSAGE] + "…"
    )
    return f"{message} (at {where})" if where else message


def _relevance(error: ValidationError) -> tuple[int, str]:
    # Shallowest first, so "missing required 'id'" wins over a nested detail.
    return (len(error.absolute_path), error.message)


def unchecked_patterns(schema: Any) -> list[str]:
    """`pattern` and `patternProperties` keys in `schema` that RE2 cannot compile."""
    found: list[str] = []
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            pattern = node.get("pattern")
            if isinstance(pattern, str) and _compile(pattern) is None and pattern not in found:
                found.append(pattern)
            for key in (
                node.get("patternProperties", {})
                if isinstance(node.get("patternProperties"), dict)
                else ()
            ):
                if _compile(key) is None and key not in found:
                    found.append(key)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found
