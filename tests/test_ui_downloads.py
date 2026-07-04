import asyncio
import base64

import pytest
from fastapi.testclient import TestClient

from devhub.app import create_app
from devhub.routes.ui_downloads import sanitize_filename
from devhub.models.server import RegisteredServer


def make_basic_auth_header(user: str = "admin", password: str = "admin123") -> dict[str, str]:
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


class TestSanitizeFilename:
    def test_keeps_alphanumeric_and_underscore_dash_dot(self) -> None:
        assert sanitize_filename("file-1.0.txt") == "file-1.0.txt"
        assert sanitize_filename("my_script_v2") == "my_script_v2"

    def test_replaces_special_chars_with_underscore(self) -> None:
        assert sanitize_filename("file<name>.sh") == "file_name_.sh"
        assert sanitize_filename("path/to/file") == "path_to_file"
        assert sanitize_filename("file with spaces") == "file_with_spaces"

    def test_empty_input_returns_download(self) -> None:
        assert sanitize_filename("") == "download"


class TestDownloadEndpoint:
    def test_download_direct_mode_with_bearer_token(self) -> None:
        server = RegisteredServer(
            id="srv-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-123/tool/my-tool/download",
            data={"mode": "direct", "arg1": "value1"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        assert "text/x-shellscript" in response.headers["content-type"]
        assert "attachment; filename=" in response.headers["content-disposition"]

        content = response.text
        assert content.startswith("#!/usr/bin/env bash")
        assert "my-tool" in content
        assert "Authorization: Bearer $AUTH_HEADER_VALUE" in content
        assert "${MCP_BEARER_TOKEN:-}" in content

    def test_download_direct_mode_with_oauth(self) -> None:
        server = RegisteredServer(
            id="srv-456",
            url="http://oauth.example.com/mcp",
            name="OAuth Server",
            auth_type="oauth",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-456/tool/my-tool/download",
            data={"mode": "direct"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content = response.text
        assert "Authorization: Bearer $AUTH_HEADER_VALUE" in content
        assert "${MCP_ACCESS_TOKEN:-}" in content
        assert "secret-token" not in content

    def test_download_direct_mode_no_auth(self) -> None:
        server = RegisteredServer(
            id="srv-789",
            url="http://noauth.example.com/mcp",
            name="No Auth Server",
            auth_type="",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-789/tool/my-tool/download",
            data={"mode": "direct"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content = response.text
        assert "AUTH_HEADER_VALUE=''" in content

    def test_download_hub_mode(self) -> None:
        server = RegisteredServer(
            id="srv-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        headers = make_basic_auth_header("admin", "admin123")
        headers["Host"] = "127.0.0.1:8080"
        response = client.post(
            "/ui/server/srv-123/tool/my-tool/download",
            data={"mode": "hub"},
            headers=headers,
        )

        assert response.status_code == 200
        content = response.text

        assert 'IS_HUB="true"' in content
        assert "X-MCP-Target-Server: srv-123" in content or "TARGET_SERVER_ID='srv-123'" in content

    def test_download_hub_mode_prompts_for_devhub_auth(self) -> None:
        server = RegisteredServer(
            id="srv-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        headers = make_basic_auth_header("admin", "admin123")
        headers["Host"] = "127.0.0.1:8080"
        response = client.post(
            "/ui/server/srv-123/tool/my-tool/download",
            data={"mode": "hub"},
            headers=headers,
        )

        assert response.status_code == 200
        content = response.text

        assert "prompt_devhub_auth" in content
        assert "DevHub Username" in content or "DEVHUB_USER" in content

    def test_download_invalid_mode_returns_400(self) -> None:
        server = RegisteredServer(
            id="srv-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-123/tool/my-tool/download",
            data={"mode": "invalid_mode"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 400

    def test_download_invalid_json_args_returns_400(self) -> None:
        server = RegisteredServer(
            id="srv-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-123/tool/my-tool/download",
            data={"__json__config": "1", "config": "not valid json"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 400

    def test_download_missing_server_returns_404(self) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/nonexistent/tool-name/download",
            data={},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 404

    def test_download_filename_sanitization(self) -> None:
        server = RegisteredServer(
            id="srv-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-123/tool/my-tool/download",
            data={},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content_disposition = response.headers["content-disposition"]
        assert "mcp-srv-123-my-tool.sh" in content_disposition

    def test_download_filename_sanitization_special_chars(self) -> None:
        assert sanitize_filename("srv<123>") == "srv_123_"
        assert sanitize_filename("tool name") == "tool_name"

    def test_download_includes_initialize_request(self) -> None:
        server = RegisteredServer(
            id="srv-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-123/tool/my-tool/download",
            data={},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content = response.text
        assert "initialize" in content
        assert "dev-hub-ui" in content
        assert "0.1.0" in content

    def test_download_includes_tool_call_request(self) -> None:
        server = RegisteredServer(
            id="srv-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-123/tool/my-tool/download",
            data={"arg1": "value1", "arg2": "42"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content = response.text
        assert "tools/call" in content or "my-tool" in content
        assert "arg1" in content
        assert "value1" in content

    def test_download_default_mode_is_direct(self) -> None:
        server = RegisteredServer(
            id="srv-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-123/tool/my-tool/download",
            data={},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content = response.text
        assert "http://example.com/mcp" in content

    def test_download_hub_mode_uses_mcp_path(self) -> None:
        server = RegisteredServer(
            id="srv-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        headers = make_basic_auth_header("admin", "admin123")
        headers["Host"] = "127.0.0.1:8080"
        response = client.post(
            "/ui/server/srv-123/tool/my-tool/download",
            data={"mode": "hub"},
            headers=headers,
        )

        assert response.status_code == 200
        content = response.text
        assert "/mcp" in content
        assert "http://example.com/mcp" not in content

    def test_download_respects_protocol_version(self) -> None:
        server = RegisteredServer(
            id="srv-proto",
            url="http://example.com/mcp",
            name="Protocol Test",
            mcp_protocol_version="2024-01-01",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-proto/tool/my-tool/download",
            data={},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content = response.text
        assert "PROTOCOL_VERSION='2024-01-01'" in content


class TestDownloadPythonEndpoint:
    def test_download_python_non_streamable_imports_mcp(self) -> None:
        server = RegisteredServer(
            id="srv-py-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-py-123/tool/my-tool/download-python",
            data={"mode": "direct", "arg1": "value1"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        assert "text/x-python" in response.headers["content-type"]
        assert "attachment; filename=" in response.headers["content-disposition"]

        content = response.text
        assert content.startswith("#!/usr/bin/env python3")
        assert "from mcp.client.session import ClientSession" in content
        assert "await session.call_tool" in content
        assert "mcp-srv-py-123-my-tool.py" in response.headers["content-disposition"]

    def test_download_python_streamable_uses_direct_http(self) -> None:
        server = RegisteredServer(
            id="srv-py-stream",
            url="http://example.com/mcp",
            name="Stream Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="sse",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-py-stream/tool/my-tool/download-python",
            data={"mode": "direct"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content = response.text
        assert "import httpx" in content
        assert "from mcp.client.session" not in content
        assert "http_request" in content

    def test_download_python_invalid_mode_returns_400(self) -> None:
        server = RegisteredServer(
            id="srv-py-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-py-123/tool/my-tool/download-python",
            data={"mode": "invalid_mode"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 400

    def test_download_python_invalid_json_args_returns_400(self) -> None:
        server = RegisteredServer(
            id="srv-py-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-py-123/tool/my-tool/download-python",
            data={"__json__config": "1", "config": "not valid json"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 400

    def test_download_python_missing_server_returns_404(self) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/nonexistent/tool-name/download-python",
            data={},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 404

    def test_download_python_filename_sanitization(self) -> None:
        server = RegisteredServer(
            id="srv-py-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-py-123/tool/my-tool/download-python",
            data={},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content_disposition = response.headers["content-disposition"]
        assert "mcp-srv-py-123-my-tool.py" in content_disposition

    def test_download_python_hub_mode(self) -> None:
        server = RegisteredServer(
            id="srv-py-hub",
            url="http://example.com/mcp",
            name="Hub Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="sse",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        headers = make_basic_auth_header("admin", "admin123")
        headers["Host"] = "127.0.0.1:8080"
        response = client.post(
            "/ui/server/srv-py-hub/tool/my-tool/download-python",
            data={"mode": "hub"},
            headers=headers,
        )

        assert response.status_code == 200
        content = response.text

        assert "IS_HUB = True" in content
        assert "X-MCP-Target-Server" in content

    def test_download_python_default_mode_is_direct(self) -> None:
        server = RegisteredServer(
            id="srv-py-123",
            url="http://example.com/mcp",
            name="Test Server",
            bearer_token="secret-token-123",
            auth_type="bearer",
            mcp_transport="http",
        )
        app = create_app()
        registry = app.state.registry

        asyncio.run(registry.register(server))

        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-py-123/tool/my-tool/download-python",
            data={},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content = response.text
        assert "http://example.com/mcp" in content
