import pytest

from devhub.mcp.constants import PROTOCOL_VERSION
from devhub.mcp.jsonrpc import (
    build_call_tool_request,
    build_initialize_request,
    build_initialized_notification,
    build_list_request,
    build_request,
    is_notification,
    validate_request,
    validate_response,
)


class TestBuildInitializeRequest:
    def test_protocol_version_in_params(self) -> None:
        result = build_initialize_request()
        assert result["params"]["protocolVersion"] == PROTOCOL_VERSION

    def test_protocol_version_is_expected_value(self) -> None:
        result = build_initialize_request()
        assert result["params"]["protocolVersion"] == "2025-11-25"


class TestBuildInitializedNotification:
    def test_has_no_id_key(self) -> None:
        result = build_initialized_notification()
        assert "id" not in result

    def test_returns_correct_jsonrpc_and_method(self) -> None:
        result = build_initialized_notification()
        assert result == {"jsonrpc": "2.0", "method": "notifications/initialized"}


class TestBuildRequest:
    def test_ping_with_id(self) -> None:
        result = build_request("ping", 1)
        assert result == {"jsonrpc": "2.0", "method": "ping", "id": 1}

    def test_notifications_initialized_with_none_id_has_no_id_key(self) -> None:
        result = build_request("notifications/initialized", None)
        assert "id" not in result


class TestBuildListRequest:
    def test_tools_list_params_is_empty_object(self) -> None:
        result = build_list_request("tools/list", 2)
        assert result["params"] == {}

    def test_prompts_list_params_is_empty_object(self) -> None:
        result = build_list_request("prompts/list", 3)
        assert result["params"] == {}

    def test_resources_list_params_is_empty_object(self) -> None:
        result = build_list_request("resources/list", 4)
        assert result["params"] == {}


class TestBuildCallToolRequest:
    def test_params_contains_name_and_arguments(self) -> None:
        result = build_call_tool_request("t", {"a": 1}, 3)
        assert result["params"] == {"name": "t", "arguments": {"a": 1}}

    def test_method_is_tools_call(self) -> None:
        result = build_call_tool_request("test_tool", {}, 1)
        assert result["method"] == "tools/call"


class TestIsNotification:
    def test_notifications_initialized_is_notification(self) -> None:
        assert is_notification("notifications/initialized") is True

    def test_notifications_prefix_is_notification(self) -> None:
        assert is_notification("notifications/test") is True

    def test_non_notification_returns_false(self) -> None:
        assert is_notification("tools/list") is False

    def test_initialize_is_not_notification(self) -> None:
        assert is_notification("initialize") is False


class TestValidateRequest:
    def test_valid_request_returns_empty_list(self) -> None:
        result = validate_request({"jsonrpc": "2.0", "method": "tools/list", "id": 1})
        assert result == []

    def test_valid_notification_returns_empty_list(self) -> None:
        result = validate_request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert result == []

    def test_notification_with_id_returns_error(self) -> None:
        result = validate_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "id": 1}
        )
        assert len(result) == 1
        assert result[0].field == "id"

    def test_invalid_jsonrpc_version_returns_error(self) -> None:
        result = validate_request({"jsonrpc": "1.0", "method": "tools/list", "id": 1})
        assert len(result) == 1
        assert result[0].field == "jsonrpc"

    def test_non_notification_missing_id_returns_error(self) -> None:
        result = validate_request({"jsonrpc": "2.0", "method": "tools/list"})
        assert len(result) >= 1
        id_errors = [e for e in result if e.field == "id"]
        assert len(id_errors) == 1

    def test_unknown_method_returns_warning(self) -> None:
        result = validate_request({"jsonrpc": "2.0", "method": "unknown/method", "id": 1})
        method_errors = [e for e in result if e.field == "method"]
        assert len(method_errors) == 1
        assert method_errors[0].severity == "warning"

    def test_invalid_json_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            validate_request(b"not json")


class TestValidateResponse:
    def test_response_with_both_result_and_error_returns_error(self) -> None:
        result = validate_response({"jsonrpc": "2.0", "result": {}, "error": {}})
        result_error_fields = [e for e in result if e.field == "result/error"]
        assert len(result_error_fields) == 1

    def test_response_with_neither_result_nor_error_returns_error(self) -> None:
        result = validate_response({"jsonrpc": "2.0"})
        assert len(result) == 1
        assert result[0].field == "result/error"

    def test_valid_response_with_result(self) -> None:
        result = validate_response({"jsonrpc": "2.0", "result": {}, "id": 1})
        assert result == []

    def test_valid_response_with_error(self) -> None:
        result = validate_response(
            {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid Request"}, "id": 1}
        )
        assert result == []

    def test_error_with_zero_code_returns_error(self) -> None:
        result = validate_response(
            {"jsonrpc": "2.0", "error": {"code": 0, "message": "Error"}, "id": 1}
        )
        code_errors = [e for e in result if e.field == "error.code"]
        assert len(code_errors) == 1

    def test_error_missing_code_returns_error(self) -> None:
        result = validate_response({"jsonrpc": "2.0", "error": {"message": "Error"}, "id": 1})
        code_errors = [e for e in result if e.field == "error.code"]
        assert len(code_errors) == 1

    def test_error_missing_message_returns_error(self) -> None:
        result = validate_response({"jsonrpc": "2.0", "error": {"code": -32600}, "id": 1})
        msg_errors = [e for e in result if e.field == "error.message"]
        assert len(msg_errors) == 1

    def test_invalid_json_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            validate_response(b"not json")
