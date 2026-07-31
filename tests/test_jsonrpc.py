import pytest

from mcp_hub.mcp.constants import PROTOCOL_VERSION, STATELESS_PROTOCOL_VERSION
from mcp_hub.mcp.jsonrpc import (
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


class TestValidateRequestVersionAware:
    def test_ping_valid_without_version(self) -> None:
        result = validate_request({"jsonrpc": "2.0", "method": "ping", "id": 1})
        assert result == []

    def test_ping_valid_for_handshake_version(self) -> None:
        result = validate_request(
            {"jsonrpc": "2.0", "method": "ping", "id": 1},
            protocol_version=PROTOCOL_VERSION,
        )
        assert result == []

    def test_ping_warns_for_stateless_version(self) -> None:
        result = validate_request(
            {"jsonrpc": "2.0", "method": "ping", "id": 1},
            protocol_version=STATELESS_PROTOCOL_VERSION,
        )
        method_errors = [e for e in result if e.field == "method"]
        assert len(method_errors) == 1
        assert method_errors[0].severity == "warning"

    def test_initialize_warns_for_stateless_version(self) -> None:
        result = validate_request(
            {"jsonrpc": "2.0", "method": "initialize", "id": 1},
            protocol_version=STATELESS_PROTOCOL_VERSION,
        )
        method_errors = [e for e in result if e.field == "method"]
        assert len(method_errors) == 1
        assert method_errors[0].severity == "warning"

    def test_logging_set_level_warns_for_stateless_version(self) -> None:
        result = validate_request(
            {"jsonrpc": "2.0", "method": "logging/setLevel", "id": 1},
            protocol_version=STATELESS_PROTOCOL_VERSION,
        )
        method_errors = [e for e in result if e.field == "method"]
        assert len(method_errors) == 1

    def test_server_discover_valid_for_stateless_version(self) -> None:
        result = validate_request(
            {"jsonrpc": "2.0", "method": "server/discover", "id": 1},
            protocol_version=STATELESS_PROTOCOL_VERSION,
        )
        assert result == []

    def test_server_discover_warns_for_handshake_version(self) -> None:
        result = validate_request(
            {"jsonrpc": "2.0", "method": "server/discover", "id": 1},
            protocol_version=PROTOCOL_VERSION,
        )
        method_errors = [e for e in result if e.field == "method"]
        assert len(method_errors) == 1
        assert method_errors[0].severity == "warning"

    def test_server_discover_valid_without_version(self) -> None:
        """No negotiated version → union behavior: methods from any revision pass."""
        result = validate_request({"jsonrpc": "2.0", "method": "server/discover", "id": 1})
        assert result == []

    def test_subscriptions_listen_valid_for_stateless_version(self) -> None:
        result = validate_request(
            {"jsonrpc": "2.0", "method": "subscriptions/listen", "id": 1},
            protocol_version=STATELESS_PROTOCOL_VERSION,
        )
        assert result == []

    def test_unknown_version_uses_union(self) -> None:
        result = validate_request(
            {"jsonrpc": "2.0", "method": "ping", "id": 1},
            protocol_version="2099-01-01",
        )
        assert result == []


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


class TestValidateResponseResultType:
    def test_result_without_result_type_is_valid(self) -> None:
        """Spec rule: results from earlier-protocol servers that omit resultType
        MUST be treated as 'complete' — never flagged."""
        result = validate_response({"jsonrpc": "2.0", "result": {"tools": []}, "id": 1})
        assert result == []

    def test_result_type_complete_is_valid(self) -> None:
        result = validate_response(
            {"jsonrpc": "2.0", "result": {"resultType": "complete", "tools": []}, "id": 1}
        )
        assert result == []

    def test_result_type_input_required_is_valid(self) -> None:
        result = validate_response(
            {
                "jsonrpc": "2.0",
                "result": {"resultType": "input_required", "inputRequests": []},
                "id": 1,
            }
        )
        assert result == []

    def test_unknown_result_type_warns(self) -> None:
        result = validate_response({"jsonrpc": "2.0", "result": {"resultType": "bogus"}, "id": 1})
        type_errors = [e for e in result if e.field == "result.resultType"]
        assert len(type_errors) == 1
        assert type_errors[0].severity == "warning"

    def test_non_string_result_type_warns(self) -> None:
        result = validate_response({"jsonrpc": "2.0", "result": {"resultType": 7}, "id": 1})
        type_errors = [e for e in result if e.field == "result.resultType"]
        assert len(type_errors) == 1

    def test_non_dict_result_is_not_inspected(self) -> None:
        result = validate_response({"jsonrpc": "2.0", "result": "ok", "id": 1})
        assert result == []
