import copy
from typing import Any

MAX_REF_DEPTH = 8

MERGE_KEYS = frozenset(
    [
        "type",
        "title",
        "description",
        "default",
        "enum",
        "oneOf",
        "anyOf",
        "items",
        "properties",
        "required",
    ]
)


def _get_defs(schema: dict[str, Any]) -> dict[str, Any] | None:
    if "$defs" in schema:
        return schema["$defs"]
    if "definitions" in schema:
        return schema["definitions"]
    return None


def _resolve_ref(
    defs: dict[str, Any],
    ref: str,
    depth: int,
) -> dict[str, Any] | None:
    if depth > MAX_REF_DEPTH:
        return None

    if not isinstance(ref, str):
        return None

    if ref.startswith("#/$defs/"):
        def_name = ref[8:]
    elif ref.startswith("#/definitions/"):
        def_name = ref[14:]
    else:
        return None

    definition = defs.get(def_name)
    if not isinstance(definition, dict):
        return None

    resolved: dict[str, Any] = {}
    for key in MERGE_KEYS:
        if key in definition:
            value = definition[key]
            if isinstance(value, dict):
                resolved[key] = _deep_copy_dict(value)
            elif isinstance(value, list):
                resolved[key] = copy.deepcopy(value)
            else:
                resolved[key] = value

    if "$ref" in definition:
        nested_ref = definition["$ref"]
        nested_resolved = _resolve_ref(defs, nested_ref, depth + 1)
        if nested_resolved:
            for key, value in nested_resolved.items():
                if key not in resolved:
                    resolved[key] = value

    return resolved


def _deep_copy_dict(d: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in d.items():
        if isinstance(value, dict):
            result[key] = _deep_copy_dict(value)
        elif isinstance(value, list):
            result[key] = list(value)
        else:
            result[key] = value
    return result


def _merge_into_property(property_dict: dict[str, Any], resolved: dict[str, Any]) -> None:
    for key in MERGE_KEYS:
        if key in resolved and key not in property_dict:
            property_dict[key] = resolved[key]


def resolve_tool_schema_refs(tools: list[dict[str, Any]]) -> None:
    for tool in tools:
        input_schema = tool.get("inputSchema")
        if not isinstance(input_schema, dict):
            continue

        defs = _get_defs(input_schema)
        if defs is None:
            continue

        properties = input_schema.get("properties")
        if not isinstance(properties, dict):
            continue

        for prop_name, prop_value in properties.items():
            if not isinstance(prop_value, dict):
                continue

            ref = prop_value.get("$ref")
            if not isinstance(ref, str):
                continue

            if not (ref.startswith("#/$defs/") or ref.startswith("#/definitions/")):
                continue

            resolved = _resolve_ref(defs, ref, 1)
            if resolved:
                _merge_into_property(prop_value, resolved)
