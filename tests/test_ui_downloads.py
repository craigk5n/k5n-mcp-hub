import asyncio
import base64

import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.routes.ui_downloads import sanitize_filename
from mcp_hub.models.server import RegisteredServer


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
    def test_download_tool_name_with_slash(self) -> None:
        # Regression: MCP tool names may contain "/" (e.g. "webcalendar/list-events").
        # The route must capture the whole name (trailing :path) and generate a script;
        # the "/" is replaced in the download filename.
        server = RegisteredServer(
            id="srv-123",
            url="http://example.com/mcp",
            name="Test Server",
            mcp_transport="http",
        )
        app = create_app()
        asyncio.run(app.state.registry.register(server))
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-123/tool-download/webcalendar/list-events",
            data={"mode": "direct"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        assert "text/x-shellscript" in response.headers["content-type"]
        content = response.text
        assert "webcalendar/list-events" in content  # real name kept in the script body
        # filename has the slash sanitized out
        assert "webcalendar_list-events" in response.headers["content-disposition"]

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
            "/ui/server/srv-123/tool-download/my-tool",
            data={"mode": "direct", "arg1": "value1"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        assert "text/x-shellscript" in response.headers["content-type"]
        assert "attachment; filename=" in response.headers["content-disposition"]

        content = response.text
        assert content.startswith("#!/usr/bin/env bash")
        assert "my-tool" in content
        assert "AUTH_TYPE='bearer'" in content
        assert "${MCP_BEARER_TOKEN:-}" in content
        assert "Authorization: Bearer ${MCP_BEARER_TOKEN}" in content
        assert "secret-token-123" not in content  # token comes from env, never embedded

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
            "/ui/server/srv-456/tool-download/my-tool",
            data={"mode": "direct"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content = response.text
        assert "AUTH_TYPE='oauth'" in content
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
            "/ui/server/srv-789/tool-download/my-tool",
            data={"mode": "direct"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content = response.text
        # No-auth server: no auth type, so no Authorization header is added at call time.
        assert "AUTH_TYPE=''" in content

    def test_download_direct_mode_basic_auth(self) -> None:
        server = RegisteredServer(
            id="srv-basic",
            url="http://basic.example.com/mcp",
            name="Basic Server",
            auth_type="basic",
            basic_username="admin",
            basic_password="super-secret-pw",
            mcp_transport="http",
        )
        app = create_app()
        asyncio.run(app.state.registry.register(server))
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/srv-basic/tool-download/my-tool",
            data={"mode": "direct"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content = response.text
        assert "AUTH_TYPE='basic'" in content
        # Username (not secret) is embedded as the default; password comes from env/prompt.
        assert "BASIC_USER_DEFAULT='admin'" in content
        assert "MCP_BASIC_PASS" in content
        assert "Authorization: Basic" in content
        assert "super-secret-pw" not in content  # password never embedded

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
            "/ui/server/srv-123/tool-download/my-tool",
            data={"mode": "hub"},
            headers=headers,
        )

        assert response.status_code == 200
        content = response.text

        assert "X-MCP-Target-Server: $TARGET_SERVER_ID" in content
        assert "TARGET_SERVER_ID='srv-123'" in content

    def test_download_hub_mode_prompts_for_mcp_hub_auth(self) -> None:
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
            "/ui/server/srv-123/tool-download/my-tool",
            data={"mode": "hub"},
            headers=headers,
        )

        assert response.status_code == 200
        content = response.text

        assert "k5n-mcp-hub Username" in content
        assert "MCPHUB_USER" in content

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
            "/ui/server/srv-123/tool-download/my-tool",
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
            "/ui/server/srv-123/tool-download/my-tool",
            data={"__json__config": "1", "config": "not valid json"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 400

    def test_download_missing_server_returns_404(self) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/nonexistent/tool-download/tool-name",
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
            "/ui/server/srv-123/tool-download/my-tool",
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
            "/ui/server/srv-123/tool-download/my-tool",
            data={},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content = response.text
        assert "initialize" in content
        assert "k5n-mcp-hub-ui" in content
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
            "/ui/server/srv-123/tool-download/my-tool",
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
            "/ui/server/srv-123/tool-download/my-tool",
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
            "/ui/server/srv-123/tool-download/my-tool",
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
            "/ui/server/srv-proto/tool-download/my-tool",
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
            "/ui/server/srv-py-123/tool-download-python/my-tool",
            data={"mode": "direct", "arg1": "value1"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        assert "text/x-python" in response.headers["content-type"]
        assert "attachment; filename=" in response.headers["content-disposition"]

        content = response.text
        assert content.startswith("#!/usr/bin/env python3")
        # No SDK dependency (mcp requires Python 3.10+); the script uses httpx directly.
        assert "import mcp" not in content
        assert "from mcp" not in content
        assert "import httpx" in content
        assert "def build_auth_header():" in content
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
            "/ui/server/srv-py-stream/tool-download-python/my-tool",
            data={"mode": "direct"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content = response.text
        assert "import httpx" in content
        assert "from mcp.client.session" not in content
        assert "build_headers" in content
        assert 'AUTH_TYPE = "bearer"' in content

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
            "/ui/server/srv-py-123/tool-download-python/my-tool",
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
            "/ui/server/srv-py-123/tool-download-python/my-tool",
            data={"__json__config": "1", "config": "not valid json"},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 400

    def test_download_python_missing_server_returns_404(self) -> None:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/nonexistent/tool-download-python/tool-name",
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
            "/ui/server/srv-py-123/tool-download-python/my-tool",
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
            "/ui/server/srv-py-hub/tool-download-python/my-tool",
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
            "/ui/server/srv-py-123/tool-download-python/my-tool",
            data={},
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 200
        content = response.text
        assert "http://example.com/mcp" in content


class TestStatelessDownloads:
    """Scripts for 2026-07-28 servers use the stateless single-POST flow —
    no initialize handshake, `_meta` in the call params, Mcp-Method header."""

    def _download(self, path: str) -> str:
        server = RegisteredServer(
            id="srv-sl",
            url="http://example.com/mcp",
            name="Stateless Server",
            mcp_transport="sse",
            mcp_protocol_version="2026-07-28",
        )
        app = create_app()
        asyncio.run(app.state.registry.register(server))
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            path,
            data={"mode": "direct"},
            headers=make_basic_auth_header("admin", "admin123"),
        )
        assert response.status_code == 200
        return response.text

    def test_shell_script_is_stateless(self) -> None:
        content = self._download("/ui/server/srv-sl/tool-download/echo")
        assert "Initializing MCP session" not in content
        assert (
            "notifications/initialized" not in content
            or "INITED_BODY" not in content.split("build_auth_header")[-1]
        )
        assert "Mcp-Method: tools/call" in content
        assert "io.modelcontextprotocol/protocolVersion" in content
        assert "2026-07-28" in content

    def test_python_script_is_stateless(self) -> None:
        content = self._download("/ui/server/srv-sl/tool-download-python/echo")
        assert "Initializing MCP session" not in content
        assert '"Mcp-Method"' in content
        assert "io.modelcontextprotocol/protocolVersion" in content

    def test_legacy_server_scripts_unchanged(self) -> None:
        server = RegisteredServer(
            id="srv-lg",
            url="http://example.com/mcp",
            name="Legacy Server",
            mcp_transport="http",
            mcp_protocol_version="2025-11-25",
        )
        app = create_app()
        asyncio.run(app.state.registry.register(server))
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/ui/server/srv-lg/tool-download/echo",
            data={"mode": "direct"},
            headers=make_basic_auth_header("admin", "admin123"),
        )
        assert response.status_code == 200
        assert "Initializing MCP session" in response.text
        assert "io.modelcontextprotocol/protocolVersion" not in response.text
