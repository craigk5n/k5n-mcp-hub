import pytest
from typing import Any
from datetime import datetime

from fastapi.testclient import TestClient

from devhub.app import create_app
from devhub.mcp.discovery import DiscoveryService
from devhub.mcp.sdk_client import InitializeResult
from devhub.models.server import RegisteredServer
from devhub.registry.service import Registry
from devhub.utils import utcnow


def make_server(
    id: str = "test-id",
    url: str = "https://test.example.com/mcp",
    tools: list[dict[str, Any]] | None = None,
) -> RegisteredServer:
    return RegisteredServer(
        id=id,
        url=url,
        name="Test Server",
        tools=tools,
    )


class MockMCPClient:
    def __init__(
        self,
        base_url: str,
        *,
        server: RegisteredServer | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url
        self.server = server
        self._tools = tools or []
        self._initialize_result: InitializeResult | None = None

    @property
    def initialize_result(self) -> InitializeResult | None:
        return self._initialize_result

    async def __aenter__(self) -> "MockMCPClient":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass

    async def handshake(self) -> InitializeResult:
        self._initialize_result = InitializeResult(
            server_name="test-server",
            server_version="1.0.0",
            protocol_version="2025-11-25",
            session_id="test-session",
            transport="http",
        )
        return self._initialize_result

    async def list(self, method: str) -> Any:
        if method == "tools/list":
            return {"tools": self._tools}
        if method == "prompts/list":
            return {"prompts": []}
        if method == "resources/list":
            return {"resources": []}
        return {}


def test_refresh_capabilities_returns_404_when_server_not_found() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/ui/server/nonexistent-id/refresh-capabilities")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_refresh_capabilities_returns_400_on_discover_failure() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    async def mock_discover_immediately_fail(
        server: RegisteredServer, *, timeout: float = 30.0
    ) -> None:
        raise RuntimeError("Connection refused")

    app.state.discovery_service.discover_immediately = mock_discover_immediately_fail  # type: ignore[method-assign]

    server = make_server(id="server-fail", url="https://fail.example.com/mcp")
    await app.state.registry.register(server)

    response = client.post("/ui/server/server-fail/refresh-capabilities")

    assert response.status_code == 400
    assert "Connection refused" in response.text


@pytest.mark.asyncio
async def test_refresh_capabilities_returns_204_on_success() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    tools_response = [{"name": "test-tool", "inputSchema": {"type": "object", "properties": {}}}]

    async def mock_discover_immediately_success(
        server: RegisteredServer, *, timeout: float = 30.0
    ) -> None:
        server.tools = tools_response

    app.state.discovery_service.discover_immediately = mock_discover_immediately_success  # type: ignore[method-assign]

    server = make_server(id="server-success", url="https://success.example.com/mcp")
    await app.state.registry.register(server)

    response = client.post("/ui/server/server-success/refresh-capabilities")

    assert response.status_code == 204


@pytest.mark.asyncio
async def test_refresh_capabilities_updates_tools_for_subsequent_get() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    new_tools = [
        {"name": "new-tool-1", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "new-tool-2", "inputSchema": {"type": "object", "properties": {}}},
    ]

    async def mock_discover_immediately_updates(
        server: RegisteredServer, *, timeout: float = 30.0
    ) -> None:
        server.tools = new_tools
        await app.state.registry.register(server)

    app.state.discovery_service.discover_immediately = mock_discover_immediately_updates  # type: ignore[method-assign]

    server = make_server(id="server-update", url="https://update.example.com/mcp", tools=[])
    await app.state.registry.register(server)

    response = client.post("/ui/server/server-update/refresh-capabilities")
    assert response.status_code == 204

    get_response = client.get("/ui/server/server-update/tools")
    assert get_response.status_code == 200
    html = get_response.text
    assert "new-tool-1" in html
    assert "new-tool-2" in html


def test_get_server_tools_returns_404_when_server_not_found() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/server/nonexistent-id/tools")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_server_tools_first_call_populates_cache_fields() -> None:
    from unittest.mock import patch, AsyncMock

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    tools_response = [{"name": "test-tool", "inputSchema": {"type": "object", "properties": {}}}]

    with patch("devhub.routes.ui_capabilities.MCPClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.handshake = AsyncMock()
        mock_client.list = AsyncMock(
            side_effect=[
                {"tools": tools_response},
                {"prompts": []},
                {"resources": []},
            ]
        )
        mock_client_class.return_value = mock_client

        server = RegisteredServer(
            id="server-cache-populate",
            url="https://cache-populate.example.com/mcp",
            name="Cache Populate Server",
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-cache-populate/tools")

        assert response.status_code == 200

        updated_server = await app.state.registry.get("server-cache-populate")
        assert updated_server is not None
        assert updated_server.tools == tools_response
        assert updated_server.prompts == []
        assert updated_server.resources == []
        assert updated_server.last_capability_sync is not None


@pytest.mark.asyncio
async def test_server_tools_second_call_returns_from_cache() -> None:
    from unittest.mock import patch, AsyncMock

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    tools_response = [{"name": "cached-tool", "inputSchema": {"type": "object", "properties": {}}}]

    server = RegisteredServer(
        id="server-cache-return",
        url="https://cache-return.example.com/mcp",
        name="Cache Return Server",
        tools=tools_response,
        prompts=[],
        resources=[],
    )
    await app.state.registry.register(server)

    with patch("devhub.routes.ui_capabilities.MCPClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.handshake = AsyncMock(
            side_effect=RuntimeError("MCPClient should not be called!")
        )
        mock_client_class.return_value = mock_client

        response = client.get("/ui/server/server-cache-return/tools")

    assert response.status_code == 200
    html = response.text
    assert "cached-tool" in html
    assert '<span class="text-gray-500">Cached:</span>' in html
    assert '<p class="text-gray-900">Yes</p>' in html


@pytest.mark.asyncio
async def test_server_tools_schema_conformance_computed_when_none() -> None:
    from unittest.mock import patch, AsyncMock

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    tools_response = [{"name": "valid-tool", "inputSchema": {"type": "object", "properties": {}}}]

    server = RegisteredServer(
        id="server-schema-compute",
        url="https://schema-compute.example.com/mcp",
        name="Schema Compute Server",
        tools=tools_response,
        schema_conformant=None,
    )
    await app.state.registry.register(server)

    response = client.get("/ui/server/server-schema-compute/tools")

    assert response.status_code == 200

    updated_server = await app.state.registry.get("server-schema-compute")
    assert updated_server is not None
    assert updated_server.schema_conformant is True
    assert updated_server.schema_issues == []


@pytest.mark.asyncio
async def test_server_tools_schema_conformance_not_recomputed_when_set() -> None:
    from unittest.mock import patch, AsyncMock

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    tools_response = [{"name": "some-tool", "inputSchema": {"type": "object", "properties": {}}}]

    server = RegisteredServer(
        id="server-schema-set",
        url="https://schema-set.example.com/mcp",
        name="Schema Set Server",
        tools=tools_response,
        schema_conformant=False,
        schema_issues=["Some existing issue"],
    )
    await app.state.registry.register(server)

    with patch("devhub.routes.ui_capabilities.validate_tool_schemas") as mock_validate:
        mock_validate.return_value = (True, [])
        response = client.get("/ui/server/server-schema-set/tools")

    assert response.status_code == 200

    updated_server = await app.state.registry.get("server-schema-set")
    assert updated_server is not None
    assert updated_server.schema_conformant is False
    assert updated_server.schema_issues == ["Some existing issue"]
    mock_validate.assert_not_called()


@pytest.mark.asyncio
async def test_server_tools_live_fetch_persists_on_success() -> None:
    from unittest.mock import patch, AsyncMock

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    tools_response = [{"name": "live-tool", "inputSchema": {"type": "object", "properties": {}}}]
    prompts_response = [{"name": "live-prompt", "description": "A live prompt"}]
    resources_response = [
        {"name": "live-resource", "uri": "test://live", "description": "A live resource"}
    ]

    with patch("devhub.routes.ui_capabilities.MCPClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.handshake = AsyncMock()
        mock_client.list = AsyncMock(
            side_effect=[
                {"tools": tools_response},
                {"prompts": prompts_response},
                {"resources": resources_response},
            ]
        )
        mock_client_class.return_value = mock_client

        server = RegisteredServer(
            id="server-live-persist",
            url="https://live-persist.example.com/mcp",
            name="Live Persist Server",
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-live-persist/tools")

        assert response.status_code == 200

        updated_server = await app.state.registry.get("server-live-persist")
        assert updated_server is not None
        assert updated_server.tools == tools_response
        assert updated_server.prompts == prompts_response
        assert updated_server.resources == resources_response
        assert updated_server.last_capability_sync is not None


@pytest.mark.asyncio
async def test_server_tools_fallback_to_discovery_on_failure() -> None:
    from unittest.mock import patch, AsyncMock

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    discovery_tools = [
        {"name": "discovery-tool", "inputSchema": {"type": "object", "properties": {}}}
    ]

    async def mock_discover_immediately(server: RegisteredServer, *, timeout: float = 30.0) -> None:
        server.tools = discovery_tools
        server.prompts = []
        server.resources = []
        server.last_capability_sync = utcnow()

    app.state.discovery_service.discover_immediately = mock_discover_immediately  # type: ignore[method-assign]

    with patch("devhub.routes.ui_capabilities.MCPClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.handshake = AsyncMock(side_effect=RuntimeError("Connection refused"))
        mock_client_class.return_value = mock_client

        server = RegisteredServer(
            id="server-fallback",
            url="https://fallback.example.com/mcp",
            name="Fallback Server",
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-fallback/tools")

    assert response.status_code == 200
    html = response.text
    assert "discovery-tool" in html


@pytest.mark.asyncio
async def test_get_server_tools_returns_empty_list_when_no_tools() -> None:
    from unittest.mock import patch, AsyncMock

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    server = RegisteredServer(
        id="server-empty",
        url="https://empty.example.com/mcp",
        name="Test Server",
    )
    await app.state.registry.register(server)

    with patch("devhub.routes.ui_capabilities.MCPClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.handshake = AsyncMock()
        mock_client.list = AsyncMock(
            side_effect=[
                {"tools": []},
                {"prompts": []},
                {"resources": []},
            ]
        )
        mock_client_class.return_value = mock_client

        response = client.get("/ui/server/server-empty/tools")

    assert response.status_code == 200
    html = response.text
    assert "No tools available" in html


@pytest.mark.asyncio
async def test_get_server_tools_returns_tools() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    tools = [{"name": "tool1", "inputSchema": {"type": "object", "properties": {}}}]
    server = make_server(id="server-tools", url="https://tools.example.com/mcp", tools=tools)
    await app.state.registry.register(server)

    response = client.get("/ui/server/server-tools/tools")

    assert response.status_code == 200
    html = response.text
    assert "tool1" in html


@pytest.mark.asyncio
async def test_get_capabilities_renders_tool_with_schema_properties() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    tools = [
        {
            "name": "test_tool",
            "title": "Test Tool",
            "description": "A test tool with properties",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "param_string": {"type": "string", "description": "A string param"},
                    "param_object": {"type": "object", "description": "An object param"},
                    "param_array": {"type": "array", "description": "An array param"},
                },
            },
        }
    ]
    server = make_server(id="server-tools", url="https://tools.example.com/mcp", tools=tools)
    await app.state.registry.register(server)

    response = client.get("/ui/server/server-tools/capabilities")

    assert response.status_code == 200
    html = response.text

    assert 'hx-post="/ui/invoke/server-tools/test_tool"' in html

    assert 'name="param_string"' in html
    assert 'name="param_object"' in html
    assert 'name="param_array"' in html

    assert 'name="__json__param_object"' in html
    assert 'name="__json__param_array"' in html
    assert "JSON" in html

    assert "/ui/invoke/server-tools/test_tool/download/bash" in html
    assert "/ui/invoke/server-tools/test_tool/download/python" in html


@pytest.mark.asyncio
async def test_get_capabilities_renders_prompts_and_resources() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    prompts = [
        {"name": "test_prompt", "description": "A test prompt"},
    ]
    resources = [
        {"name": "test_resource", "uri": "test://resource", "description": "A test resource"},
    ]
    server = RegisteredServer(
        id="server-list",
        url="https://list.example.com/mcp",
        name="List Server",
        tools=[],
        prompts=prompts,
        resources=resources,
    )
    await app.state.registry.register(server)

    response = client.get("/ui/server/server-list/capabilities")

    assert response.status_code == 200
    html = response.text

    assert "test_prompt" in html
    assert "test_resource" in html
    assert "test://resource" in html


@pytest.mark.asyncio
async def test_get_capabilities_has_refresh_button() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    server = RegisteredServer(
        id="server-refresh",
        url="https://refresh.example.com/mcp",
        name="Refresh Server",
        tools=[{"name": "test_tool", "inputSchema": {"type": "object", "properties": {}}}],
    )
    await app.state.registry.register(server)

    response = client.get("/ui/server/server-refresh/capabilities")

    assert response.status_code == 200
    html = response.text

    assert 'hx-post="/ui/server/server-refresh/refresh-capabilities"' in html
    assert "Refresh capabilities" in html


@pytest.mark.asyncio
async def test_get_capabilities_shows_schema_conformance() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    server = RegisteredServer(
        id="server-schema",
        url="https://schema.example.com/mcp",
        name="Schema Server",
        tools=[{"name": "test_tool", "inputSchema": {"type": "object", "properties": {}}}],
        schema_conformant=False,
        schema_issues=["Invalid type for tool 'bad_tool'", "Missing required property 'name'"],
    )
    await app.state.registry.register(server)

    response = client.get("/ui/server/server-schema/capabilities")

    assert response.status_code == 200
    html = response.text

    assert "Schema Conformant:" in html
    assert "No" in html
    assert "Schema Issues:" in html
    assert "Invalid type for tool" in html
    assert "Missing required property" in html


def test_get_capabilities_returns_404_when_server_not_found() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/server/nonexistent-id/capabilities")

    assert response.status_code == 404
    assert response.json() == {"detail": "Server not found"}


@pytest.mark.asyncio
async def test_capabilities_cached_path_does_not_contact_mcp_client() -> None:
    from unittest.mock import patch

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    tools = [{"name": "cached_tool", "inputSchema": {"type": "object", "properties": {}}}]
    server = RegisteredServer(
        id="server-cached",
        url="https://cached.example.com/mcp",
        name="Cached Server",
        tools=tools,
    )
    await app.state.registry.register(server)

    with patch("devhub.routes.ui_capabilities.MCPClient") as mock_client_class:
        mock_client = mock_client_class.return_value.__aenter__.return_value
        mock_client.handshake.side_effect = RuntimeError("MCPClient should not be called!")

        response = client.get("/ui/server/server-cached/capabilities")

    assert response.status_code == 200
    html = response.text
    assert "cached_tool" in html
    assert '<span class="text-gray-500">Cached:</span>' in html
    assert '<p class="text-gray-900">Yes</p>' in html


@pytest.mark.asyncio
async def test_capabilities_live_fetch_handshake_failure() -> None:
    from unittest.mock import patch

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    server = RegisteredServer(
        id="server-fail-handshake",
        url="https://fail-handshake.example.com/mcp",
        name="Fail Handshake Server",
    )
    await app.state.registry.register(server)

    with patch("devhub.routes.ui_capabilities.MCPClient") as mock_client_class:
        mock_client = mock_client_class.return_value.__aenter__.return_value
        mock_client.handshake.side_effect = RuntimeError("Connection refused")

        response = client.get("/ui/server/server-fail-handshake/capabilities")

    assert response.status_code == 200
    html = response.text
    assert "Handshake failed" in html


@pytest.mark.asyncio
async def test_capabilities_404_returns_json_error_when_server_not_found() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/server/definitely-does-not-exist-12345/capabilities")

    assert response.status_code == 404
    assert response.json() == {"detail": "Server not found"}
