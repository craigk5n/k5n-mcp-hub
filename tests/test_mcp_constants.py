import pytest

from mcp_hub.mcp.constants import (
    BACKWARD_COMPAT_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    is_supported_protocol_version,
    VALID_MCP_METHODS,
    METHOD_INITIALIZE,
    METHOD_INITIALIZED,
    METHOD_TOOLS_LIST,
    METHOD_TOOLS_CALL,
    METHOD_PROMPTS_LIST,
    METHOD_PROMPTS_GET,
    METHOD_RESOURCES_LIST,
    METHOD_RESOURCES_READ,
    METHOD_RESOURCES_TEMPLATES,
    METHOD_PING,
    METHOD_LOGGING_SET_LEVEL,
    METHOD_COMPLETION_COMPLETE,
    METHOD_SAMPLING_CREATE_MESSAGE,
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    INTERNAL_ERROR,
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    MCP_DISCOVERY_INTERVAL_SECONDS,
)


class TestProtocolVersion:
    def test_current_protocol_version(self) -> None:
        assert PROTOCOL_VERSION == "2025-11-25"

    def test_backward_compat_protocol_version(self) -> None:
        assert BACKWARD_COMPAT_PROTOCOL_VERSION == "2025-06-18"

    def test_supported_protocol_versions_contains_both(self) -> None:
        assert PROTOCOL_VERSION in SUPPORTED_PROTOCOL_VERSIONS
        assert BACKWARD_COMPAT_PROTOCOL_VERSION in SUPPORTED_PROTOCOL_VERSIONS

    def test_is_supported_protocol_version_current(self) -> None:
        assert is_supported_protocol_version("2025-11-25") is True

    def test_is_supported_protocol_version_backward_compat(self) -> None:
        assert is_supported_protocol_version("2025-06-18") is True

    def test_is_supported_protocol_version_unsupported(self) -> None:
        assert is_supported_protocol_version("1.0") is False

    def test_is_supported_protocol_version_strips_whitespace(self) -> None:
        assert is_supported_protocol_version(" 2025-11-25 ") is True


class TestMcpMethods:
    def test_all_13_methods_in_valid_mcp_methods(self) -> None:
        expected_methods = {
            METHOD_INITIALIZE,
            METHOD_INITIALIZED,
            METHOD_TOOLS_LIST,
            METHOD_TOOLS_CALL,
            METHOD_PROMPTS_LIST,
            METHOD_PROMPTS_GET,
            METHOD_RESOURCES_LIST,
            METHOD_RESOURCES_READ,
            METHOD_RESOURCES_TEMPLATES,
            METHOD_PING,
            METHOD_LOGGING_SET_LEVEL,
            METHOD_COMPLETION_COMPLETE,
            METHOD_SAMPLING_CREATE_MESSAGE,
        }
        assert VALID_MCP_METHODS == expected_methods

    def test_valid_mcp_methods_is_frozenset(self) -> None:
        assert isinstance(VALID_MCP_METHODS, frozenset)


class TestJsonRpcErrorCodes:
    def test_parse_error(self) -> None:
        assert PARSE_ERROR == -32700

    def test_invalid_request(self) -> None:
        assert INVALID_REQUEST == -32600

    def test_method_not_found(self) -> None:
        assert METHOD_NOT_FOUND == -32601

    def test_invalid_params(self) -> None:
        assert INVALID_PARAMS == -32602

    def test_internal_error(self) -> None:
        assert INTERNAL_ERROR == -32603


class TestClientIdentity:
    def test_mcp_client_name(self) -> None:
        assert isinstance(MCP_CLIENT_NAME, str)
        assert MCP_CLIENT_NAME == "mcp_hub"

    def test_mcp_client_version(self) -> None:
        assert isinstance(MCP_CLIENT_VERSION, str)

    def test_mcp_discovery_interval_seconds(self) -> None:
        assert isinstance(MCP_DISCOVERY_INTERVAL_SECONDS, int)
        assert MCP_DISCOVERY_INTERVAL_SECONDS == 30
