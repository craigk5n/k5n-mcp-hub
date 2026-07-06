from typing import Any


def validate_tool_name(name: str) -> list[str]:
    issues: list[str] = []
    stripped = name.strip()
    if not stripped:
        issues.append("is empty")
        return issues

    if len(name) > 64:
        issues.append(f"is longer than 64 chars ({len(name)})")

    if " " in name:
        issues.append("contains spaces")

    for ch in name:
        if ch == " ":
            continue
        if ch not in ("_", "-", ".") and not ("a" <= ch <= "z") and not ("0" <= ch <= "9"):
            issues.append(f"contains invalid character {ch!r}")

    return issues


def validate_tool_schemas(tools: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    issues: list[str] = []

    for idx, tool in enumerate(tools):
        name = tool.get("name")
        label = name if name else str(idx)

        if not name:
            issues.append(f"tool[{idx}] missing name")

        if name:
            name_issues = validate_tool_name(name)
            for issue in name_issues:
                issues.append(f"{label} name {issue}")

        input_schema = tool.get("inputSchema", {})
        schema_type = input_schema.get("type")
        properties = input_schema.get("properties") or {}
        required = input_schema.get("required") or []

        if properties and schema_type and schema_type != "object":
            issues.append(f'{label} schema type "{schema_type}" with properties')

        if required and not properties:
            issues.append(f"{label} required list without properties")

        for req_field in required:
            if req_field not in properties:
                issues.append(f'{label} required field "{req_field}" missing from properties')

    is_conformant = len(issues) == 0
    return (is_conformant, issues)
