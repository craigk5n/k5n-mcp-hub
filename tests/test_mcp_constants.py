import pytest

from mcp_hub.mcp.constants import (
    BACKWARD_COMPAT_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    STATELESS_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    is_supported_protocol_version,
    mcp_version_status,
    resolve_protocol_version,
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
    METHOD_RESOURCES_SUBSCRIBE,
    METHOD_RESOURCES_UNSUBSCRIBE,
    METHOD_ELICITATION_CREATE,
    METHOD_ROOTS_LIST,
    METHOD_SERVER_DISCOVER,
    METHOD_SUBSCRIPTIONS_LISTEN,
    HANDSHAKE_MCP_METHODS,
    STATELESS_MCP_METHODS,
    valid_methods_for,
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    INTERNAL_ERROR,
    RESOURCE_NOT_FOUND,
    LEGACY_RESOURCE_NOT_FOUND,
    HEADER_MISMATCH,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    UNSUPPORTED_PROTOCOL_VERSION,
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    MCP_DISCOVERY_INTERVAL_SECONDS,
)


class TestProtocolVersion:
    def test_current_protocol_version(self) -> None:
        assert PROTOCOL_VERSION == "2025-11-25"

    def test_backward_compat_protocol_version(self) -> None:
        assert BACKWARD_COMPAT_PROTOCOL_VERSION == "2025-06-18"

    def test_stateless_protocol_version(self) -> None:
        assert STATELESS_PROTOCOL_VERSION == "2026-07-28"

    def test_supported_protocol_versions_contains_all(self) -> None:
        assert PROTOCOL_VERSION in SUPPORTED_PROTOCOL_VERSIONS
        assert BACKWARD_COMPAT_PROTOCOL_VERSION in SUPPORTED_PROTOCOL_VERSIONS
        assert STATELESS_PROTOCOL_VERSION in SUPPORTED_PROTOCOL_VERSIONS

    def test_is_supported_protocol_version_current(self) -> None:
        assert is_supported_protocol_version("2025-11-25") is True

    def test_is_supported_protocol_version_backward_compat(self) -> None:
        assert is_supported_protocol_version("2025-06-18") is True

    def test_is_supported_protocol_version_stateless(self) -> None:
        assert is_supported_protocol_version("2026-07-28") is True

    def test_is_supported_protocol_version_unsupported(self) -> None:
        assert is_supported_protocol_version("1.0") is False

    def test_is_supported_protocol_version_strips_whitespace(self) -> None:
        assert is_supported_protocol_version(" 2025-11-25 ") is True


class TestResolveProtocolVersion:
    def test_empty_falls_back_to_current(self) -> None:
        assert resolve_protocol_version("") == PROTOCOL_VERSION

    def test_none_falls_back_to_current(self) -> None:
        assert resolve_protocol_version(None) == PROTOCOL_VERSION

    def test_whitespace_only_falls_back_to_current(self) -> None:
        assert resolve_protocol_version("   ") == PROTOCOL_VERSION

    def test_configured_version_is_honored(self) -> None:
        assert resolve_protocol_version(BACKWARD_COMPAT_PROTOCOL_VERSION) == "2025-06-18"

    def test_strips_whitespace(self) -> None:
        assert resolve_protocol_version(" 2025-06-18 ") == "2025-06-18"

    def test_unsupported_version_is_still_honored(self) -> None:
        """A server that negotiated a version we don't recognize still gets that version
        echoed back — silently substituting our own would misreport the negotiation."""
        assert resolve_protocol_version("2024-11-05") == "2024-11-05"


class TestMcpVersionStatus:
    @pytest.mark.parametrize(
        "version",
        sorted(SUPPORTED_PROTOCOL_VERSIONS),
    )
    def test_supported_versions_are_supported(self, version: str) -> None:
        assert mcp_version_status(version) == "supported"

    def test_older_than_oldest_supported_is_outdated(self) -> None:
        assert mcp_version_status("2024-11-05") == "outdated"

    def test_newer_than_newest_supported_is_newer(self) -> None:
        """A future revision the hub doesn't know yet must not be lumped into
        'unsupported' — it gets its own status so the UI can say the hub is behind."""
        assert mcp_version_status("2099-01-01") == "newer"

    def test_unknown_version_between_supported_is_unsupported(self) -> None:
        assert mcp_version_status("2026-01-01") == "unsupported"

    def test_empty_version_is_unknown(self) -> None:
        assert mcp_version_status("") == "unknown"
        assert mcp_version_status("   ") == "unknown"


class TestMcpMethods:
    def test_handshake_methods_include_the_original_13(self) -> None:
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
        assert expected_methods <= HANDSHAKE_MCP_METHODS

    def test_handshake_methods_include_subscribe_and_client_features(self) -> None:
        assert METHOD_RESOURCES_SUBSCRIBE in HANDSHAKE_MCP_METHODS
        assert METHOD_RESOURCES_UNSUBSCRIBE in HANDSHAKE_MCP_METHODS
        assert METHOD_ELICITATION_CREATE in HANDSHAKE_MCP_METHODS
        assert METHOD_ROOTS_LIST in HANDSHAKE_MCP_METHODS

    def test_stateless_methods_add_discover_and_listen(self) -> None:
        assert METHOD_SERVER_DISCOVER in STATELESS_MCP_METHODS
        assert METHOD_SUBSCRIPTIONS_LISTEN in STATELESS_MCP_METHODS

    def test_stateless_methods_drop_removed_methods(self) -> None:
        removed = {
            METHOD_INITIALIZE,
            METHOD_INITIALIZED,
            METHOD_PING,
            METHOD_LOGGING_SET_LEVEL,
            METHOD_RESOURCES_SUBSCRIBE,
            METHOD_RESOURCES_UNSUBSCRIBE,
            METHOD_SAMPLING_CREATE_MESSAGE,
            METHOD_ELICITATION_CREATE,
            METHOD_ROOTS_LIST,
        }
        assert not removed & STATELESS_MCP_METHODS

    def test_stateless_methods_keep_core_operations(self) -> None:
        kept = {
            METHOD_TOOLS_LIST,
            METHOD_TOOLS_CALL,
            METHOD_PROMPTS_LIST,
            METHOD_PROMPTS_GET,
            METHOD_RESOURCES_LIST,
            METHOD_RESOURCES_READ,
            METHOD_RESOURCES_TEMPLATES,
            METHOD_COMPLETION_COMPLETE,
        }
        assert kept <= STATELESS_MCP_METHODS

    def test_valid_mcp_methods_is_union_of_all_revisions(self) -> None:
        assert VALID_MCP_METHODS == HANDSHAKE_MCP_METHODS | STATELESS_MCP_METHODS

    def test_valid_mcp_methods_is_frozenset(self) -> None:
        assert isinstance(VALID_MCP_METHODS, frozenset)
        assert isinstance(HANDSHAKE_MCP_METHODS, frozenset)
        assert isinstance(STATELESS_MCP_METHODS, frozenset)


class TestValidMethodsFor:
    def test_none_returns_union(self) -> None:
        assert valid_methods_for(None) == VALID_MCP_METHODS

    def test_empty_returns_union(self) -> None:
        assert valid_methods_for("") == VALID_MCP_METHODS

    def test_unknown_version_returns_union(self) -> None:
        assert valid_methods_for("2099-01-01") == VALID_MCP_METHODS

    def test_handshake_versions_return_handshake_set(self) -> None:
        assert valid_methods_for(PROTOCOL_VERSION) == HANDSHAKE_MCP_METHODS
        assert valid_methods_for(BACKWARD_COMPAT_PROTOCOL_VERSION) == HANDSHAKE_MCP_METHODS

    def test_stateless_version_returns_stateless_set(self) -> None:
        assert valid_methods_for(STATELESS_PROTOCOL_VERSION) == STATELESS_MCP_METHODS

    def test_strips_whitespace(self) -> None:
        assert valid_methods_for(f" {STATELESS_PROTOCOL_VERSION} ") == STATELESS_MCP_METHODS


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

    def test_resource_not_found_is_invalid_params(self) -> None:
        """2026-07-28 changed resource-not-found from -32002 to -32602 (Invalid Params)."""
        assert RESOURCE_NOT_FOUND == INVALID_PARAMS == -32602

    def test_legacy_resource_not_found(self) -> None:
        assert LEGACY_RESOURCE_NOT_FOUND == -32002

    def test_mcp_reserved_error_codes(self) -> None:
        """2026-07-28 reserves -32020..-32099 for the MCP spec."""
        assert HEADER_MISMATCH == -32020
        assert MISSING_REQUIRED_CLIENT_CAPABILITY == -32021
        assert UNSUPPORTED_PROTOCOL_VERSION == -32022


class TestClientIdentity:
    def test_mcp_client_name(self) -> None:
        assert isinstance(MCP_CLIENT_NAME, str)
        assert MCP_CLIENT_NAME == "mcp_hub"

    def test_mcp_client_version(self) -> None:
        assert isinstance(MCP_CLIENT_VERSION, str)

    def test_mcp_discovery_interval_seconds(self) -> None:
        assert isinstance(MCP_DISCOVERY_INTERVAL_SECONDS, int)
        assert MCP_DISCOVERY_INTERVAL_SECONDS == 30
