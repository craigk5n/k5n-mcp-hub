import json
from dataclasses import dataclass
from typing import Any, cast

from devhub.mcp.constants import (
    METHOD_INITIALIZED,
    METHOD_INITIALIZE,
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    PROTOCOL_VERSION,
    VALID_MCP_METHODS,
)


@dataclass
class ValidationError:
    field: str
    message: str
    severity: str = "error"


def is_notification(method: str) -> bool:
    return method == METHOD_INITIALIZED or method.startswith("notifications/")


def build_request(method: str, request_id: str | int | None, params: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"jsonrpc": "2.0", "method": method}

    should_omit_id = request_id is None and (
        method.startswith("notifications/") or method == METHOD_INITIALIZED
    )
    if not should_omit_id:
        result["id"] = request_id

    if params is not None:
        result["params"] = params

    return result


def build_initialize_request(
    request_id: str = "init-1",
    client_name: str = MCP_CLIENT_NAME,
    client_version: str = MCP_CLIENT_VERSION,
) -> dict[str, Any]:
    return build_request(
        METHOD_INITIALIZE,
        request_id,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": client_version},
        },
    )


def build_initialized_notification() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": METHOD_INITIALIZED}


def build_list_request(method: str, request_id: str | int) -> dict[str, Any]:
    return build_request(method, request_id, {})


def build_call_tool_request(
    tool_name: str, arguments: dict[str, Any], request_id: str | int
) -> dict[str, Any]:
    return build_request("tools/call", request_id, {"name": tool_name, "arguments": arguments})


def validate_request(data: bytes | dict) -> list[ValidationError]:
    if isinstance(data, bytes):
        try:
            loaded = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}") from e
        data = cast(dict, loaded)

    errors: list[ValidationError] = []

    jsonrpc = data.get("jsonrpc")
    if jsonrpc != "2.0":
        errors.append(ValidationError(field="jsonrpc", message='"jsonrpc" must equal "2.0"'))

    method = data.get("method")
    if not isinstance(method, str) or method == "":
        errors.append(
            ValidationError(field="method", message='"method" must be a non-empty string')
        )
    else:
        if method not in VALID_MCP_METHODS:
            errors.append(
                ValidationError(
                    field="method",
                    message=f'unknown MCP method "{method}"',
                    severity="warning",
                )
            )

    if isinstance(method, str):
        notification = is_notification(method)
        has_id = "id" in data

        if notification and has_id:
            errors.append(
                ValidationError(field="id", message="Notifications MUST NOT have an 'id' key")
            )
        elif not notification and not has_id:
            errors.append(
                ValidationError(field="id", message="Non-notifications MUST have an 'id' key")
            )

    return errors


def validate_response(data: bytes | dict) -> list[ValidationError]:
    if isinstance(data, bytes):
        try:
            loaded = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}") from e
        data = cast(dict, loaded)

    errors: list[ValidationError] = []

    jsonrpc = data.get("jsonrpc")
    if jsonrpc != "2.0":
        errors.append(ValidationError(field="jsonrpc", message='"jsonrpc" must equal "2.0"'))

    has_result = "result" in data
    has_error = "error" in data

    if has_result and has_error:
        errors.append(
            ValidationError(
                field="result/error", message="Exactly one of 'result' or 'error' must be present"
            )
        )
    elif not has_result and not has_error:
        errors.append(
            ValidationError(
                field="result/error", message="Exactly one of 'result' or 'error' must be present"
            )
        )

    if has_error:
        error_obj = data.get("error")
        if not isinstance(error_obj, dict):
            errors.append(ValidationError(field="error", message="'error' must be an object"))
        else:
            if "code" not in error_obj:
                errors.append(
                    ValidationError(field="error.code", message="'error.code' is required")
                )
            elif error_obj.get("code") == 0:
                errors.append(
                    ValidationError(field="error.code", message="'error.code' must be non-zero")
                )

            if "message" not in error_obj:
                errors.append(
                    ValidationError(field="error.message", message="'error.message' is required")
                )

    return errors
