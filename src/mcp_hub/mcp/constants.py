# The version the hub sends when it initiates an `initialize` handshake. Stays on the
# last handshake-based revision until the stateless client path lands (TODO.md Epic 2).
PROTOCOL_VERSION = "2025-11-25"
BACKWARD_COMPAT_PROTOCOL_VERSION = "2025-06-18"
# The stateless revision: no initialize/session, `server/discover`, `subscriptions/listen`.
STATELESS_PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {PROTOCOL_VERSION, BACKWARD_COMPAT_PROTOCOL_VERSION, STATELESS_PROTOCOL_VERSION}
)


def is_supported_protocol_version(version: str) -> bool:
    stripped = version.strip()
    return stripped in SUPPORTED_PROTOCOL_VERSIONS


def supported_protocol_versions_str() -> str:
    """Supported protocol versions, newest first, for display (e.g. tooltips)."""
    return ", ".join(sorted(SUPPORTED_PROTOCOL_VERSIONS, reverse=True))


def mcp_version_status(version: str) -> str:
    """Classify a server's MCP protocol version for the UI badge.

    Returns 'supported', 'outdated' (older than the oldest version this hub supports),
    'newer' (a revision released after the newest one this hub knows — the hub is
    behind, not the server), or 'unsupported' (unrecognized in between). MCP versions
    are ISO dates, so they compare lexically.
    """
    stripped = (version or "").strip()
    if not stripped:
        return "unknown"
    if stripped in SUPPORTED_PROTOCOL_VERSIONS:
        return "supported"
    if stripped < min(SUPPORTED_PROTOCOL_VERSIONS):
        return "outdated"
    if stripped > max(SUPPORTED_PROTOCOL_VERSIONS):
        return "newer"
    return "unsupported"


def resolve_protocol_version(configured: str | None) -> str:
    """Pick the ``MCP-Protocol-Version`` to send outbound to a backend.

    A server's negotiated ``mcp_protocol_version`` (set during discovery) wins so we
    echo back the version it actually agreed to; strict backends reject a header that
    doesn't match their negotiation. Falls back to our current ``PROTOCOL_VERSION`` when
    nothing has been negotiated yet."""
    if configured is None:
        return PROTOCOL_VERSION
    stripped = configured.strip()
    return stripped or PROTOCOL_VERSION


METHOD_INITIALIZE = "initialize"
METHOD_INITIALIZED = "notifications/initialized"
METHOD_TOOLS_LIST = "tools/list"
METHOD_TOOLS_CALL = "tools/call"
METHOD_PROMPTS_LIST = "prompts/list"
METHOD_PROMPTS_GET = "prompts/get"
METHOD_RESOURCES_LIST = "resources/list"
METHOD_RESOURCES_READ = "resources/read"
METHOD_RESOURCES_TEMPLATES = "resources/templates/list"
METHOD_PING = "ping"
METHOD_LOGGING_SET_LEVEL = "logging/setLevel"
METHOD_COMPLETION_COMPLETE = "completion/complete"
METHOD_SAMPLING_CREATE_MESSAGE = "sampling/createMessage"
METHOD_RESOURCES_SUBSCRIBE = "resources/subscribe"
METHOD_RESOURCES_UNSUBSCRIBE = "resources/unsubscribe"
METHOD_ELICITATION_CREATE = "elicitation/create"
METHOD_ROOTS_LIST = "roots/list"
# Introduced by the stateless 2026-07-28 revision.
METHOD_SERVER_DISCOVER = "server/discover"
METHOD_SUBSCRIPTIONS_LISTEN = "subscriptions/listen"

# Methods valid on the handshake-based revisions (2025-06-18, 2025-11-25).
HANDSHAKE_MCP_METHODS = frozenset(
    {
        METHOD_INITIALIZE,
        METHOD_INITIALIZED,
        METHOD_TOOLS_LIST,
        METHOD_TOOLS_CALL,
        METHOD_PROMPTS_LIST,
        METHOD_PROMPTS_GET,
        METHOD_RESOURCES_LIST,
        METHOD_RESOURCES_READ,
        METHOD_RESOURCES_TEMPLATES,
        METHOD_RESOURCES_SUBSCRIBE,
        METHOD_RESOURCES_UNSUBSCRIBE,
        METHOD_PING,
        METHOD_LOGGING_SET_LEVEL,
        METHOD_COMPLETION_COMPLETE,
        METHOD_SAMPLING_CREATE_MESSAGE,
        METHOD_ELICITATION_CREATE,
        METHOD_ROOTS_LIST,
    }
)

# Methods valid on the stateless 2026-07-28 revision, which removed the handshake,
# ping, logging/setLevel, resource subscriptions (replaced by subscriptions/listen),
# and server-initiated requests (sampling/elicitation/roots, replaced by MRTR).
STATELESS_MCP_METHODS = frozenset(
    {
        METHOD_SERVER_DISCOVER,
        METHOD_SUBSCRIPTIONS_LISTEN,
        METHOD_TOOLS_LIST,
        METHOD_TOOLS_CALL,
        METHOD_PROMPTS_LIST,
        METHOD_PROMPTS_GET,
        METHOD_RESOURCES_LIST,
        METHOD_RESOURCES_READ,
        METHOD_RESOURCES_TEMPLATES,
        METHOD_COMPLETION_COMPLETE,
    }
)

# Union across all supported revisions — the version-agnostic default.
VALID_MCP_METHODS = HANDSHAKE_MCP_METHODS | STATELESS_MCP_METHODS


def valid_methods_for(protocol_version: str | None) -> frozenset[str]:
    """Methods valid for a negotiated protocol version.

    Falls back to the union of all revisions when the version is unknown or not
    provided, so callers without negotiation context never see new warnings."""
    stripped = (protocol_version or "").strip()
    if stripped == STATELESS_PROTOCOL_VERSION:
        return STATELESS_MCP_METHODS
    if stripped in SUPPORTED_PROTOCOL_VERSIONS:
        return HANDSHAKE_MCP_METHODS
    return VALID_MCP_METHODS


PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# 2026-07-28 aligned resource-not-found with JSON-RPC Invalid Params; older
# revisions used -32002.
RESOURCE_NOT_FOUND = INVALID_PARAMS
LEGACY_RESOURCE_NOT_FOUND = -32002

# MCP-reserved error range (-32020..-32099), introduced in 2026-07-28.
HEADER_MISMATCH = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022

# Values of the required ``resultType`` field on 2026-07-28 results. Results
# from earlier-protocol servers that omit the field are treated as "complete".
RESULT_TYPE_COMPLETE = "complete"
RESULT_TYPE_INPUT_REQUIRED = "input_required"
VALID_RESULT_TYPES = frozenset({RESULT_TYPE_COMPLETE, RESULT_TYPE_INPUT_REQUIRED})

MCP_CLIENT_NAME = "mcp_hub"
MCP_CLIENT_VERSION = "0.1.0"
MCP_DISCOVERY_INTERVAL_SECONDS = 30
