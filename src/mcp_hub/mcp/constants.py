PROTOCOL_VERSION = "2025-11-25"
BACKWARD_COMPAT_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({PROTOCOL_VERSION, BACKWARD_COMPAT_PROTOCOL_VERSION})


def is_supported_protocol_version(version: str) -> bool:
    stripped = version.strip()
    return stripped in SUPPORTED_PROTOCOL_VERSIONS


def supported_protocol_versions_str() -> str:
    """Supported protocol versions, newest first, for display (e.g. tooltips)."""
    return ", ".join(sorted(SUPPORTED_PROTOCOL_VERSIONS, reverse=True))


def mcp_version_status(version: str) -> str:
    """Classify a server's MCP protocol version for the UI badge.

    Returns 'supported', 'outdated' (older than the oldest version this hub supports),
    or 'unsupported' (newer/unknown). MCP versions are ISO dates, so they compare
    lexically.
    """
    stripped = (version or "").strip()
    if not stripped:
        return "unknown"
    if stripped in SUPPORTED_PROTOCOL_VERSIONS:
        return "supported"
    if stripped < min(SUPPORTED_PROTOCOL_VERSIONS):
        return "outdated"
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

VALID_MCP_METHODS = frozenset(
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
        METHOD_PING,
        METHOD_LOGGING_SET_LEVEL,
        METHOD_COMPLETION_COMPLETE,
        METHOD_SAMPLING_CREATE_MESSAGE,
    }
)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

MCP_CLIENT_NAME = "mcp_hub"
MCP_CLIENT_VERSION = "0.1.0"
MCP_DISCOVERY_INTERVAL_SECONDS = 30
