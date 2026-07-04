import hashlib
import re

import pytest

from devhub.routes.ui_conformance import (
    OfficialScenario,
    parse_official_conformance_output,
    scenario_key,
)


class TestScenarioKey:
    def test_tool_calls(self) -> None:
        result = scenario_key("Tool Calls!")
        assert result.startswith("tool-calls-")
        assert len(result) == len("tool-calls-") + 8

    def test_simple_name(self) -> None:
        result = scenario_key("Initialize")
        assert result.startswith("initialize-")

    def test_spaces_and_special_chars(self) -> None:
        result = scenario_key("Hello World! Test")
        assert result.startswith("hello-world-test-")

    def test_only_special_chars(self) -> None:
        result = scenario_key("!!!")
        assert result.startswith("scenario-")

    def test_empty_name(self) -> None:
        result = scenario_key("")
        assert result.startswith("scenario-")

    def test_trims_leading_trailing_dashes(self) -> None:
        result = scenario_key("  foo  ")
        assert result.startswith("foo-")

    def test_hex_suffix_consistency(self) -> None:
        hex1 = scenario_key("Test")[-8:]
        hex2 = scenario_key("Test")[-8:]
        assert hex1 == hex2

        name_hash = hashlib.sha1(b"Test").hexdigest()[:8]
        assert hex1 == name_hash


class TestParseOfficialConformanceOutput:
    def test_sample_output(self) -> None:
        output = """=== SUMMARY ===
✓ Initialize: 5 passed, 0 failed
✗ Tool Calls: 2 passed, 3 failed
Total: 7 passed, 3 failed
"""
        scenarios, total_pass, total_fail = parse_official_conformance_output(output)

        assert len(scenarios) == 2
        assert total_pass == 7
        assert total_fail == 3

        init_scenario = next(s for s in scenarios if s.name == "Initialize")
        assert init_scenario.passed == 5
        assert init_scenario.failed == 0
        assert init_scenario.ok is True

        tool_calls_scenario = next(s for s in scenarios if s.name == "Tool Calls")
        assert tool_calls_scenario.passed == 2
        assert tool_calls_scenario.failed == 3
        assert tool_calls_scenario.ok is False

    def test_totals_from_scenarios_when_zero(self) -> None:
        output = """=== SUMMARY ===
✓ Test1: 2 passed, 1 failed
✓ Test2: 3 passed, 0 failed
"""
        scenarios, total_pass, total_fail = parse_official_conformance_output(output)

        assert len(scenarios) == 2
        assert total_pass == 5
        assert total_fail == 1

    def test_no_summary_section(self) -> None:
        output = """Some other output
without summary"""
        scenarios, total_pass, total_fail = parse_official_conformance_output(output)

        assert scenarios == []
        assert total_pass == 0
        assert total_fail == 0

    def test_empty_output(self) -> None:
        scenarios, total_pass, total_fail = parse_official_conformance_output("")
        assert scenarios == []
        assert total_pass == 0
        assert total_fail == 0

    def test_scenario_keys_are_generated(self) -> None:
        output = """=== SUMMARY ===
✓ Initialize: 5 passed, 0 failed
"""
        scenarios, _, _ = parse_official_conformance_output(output)

        assert len(scenarios) == 1
        assert scenarios[0].key == scenario_key("Initialize")


class TestGetServerConformance:
    def test_returns_404_when_server_not_found(self) -> None:
        from fastapi.testclient import TestClient
        from devhub.app import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/ui/server/nonexistent-id/conformance")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_protocol_version_supported(self) -> None:
        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-supported-protocol",
            url="https://supported.example.com/mcp",
            name="Supported Protocol Server",
            mcp_protocol_version="2025-11-25",
            tools=[{"name": "test-tool", "inputSchema": {"type": "object", "properties": {}}}],
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-supported-protocol/conformance")

        assert response.status_code == 200
        html = response.text
        assert "2025-11-25" in html

    @pytest.mark.asyncio
    async def test_oauth_status_na_for_bearer(self) -> None:
        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-bearer",
            url="https://bearer.example.com/mcp",
            name="Bearer Server",
            auth_type="bearer",
            tools=[{"name": "test-tool", "inputSchema": {"type": "object", "properties": {}}}],
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-bearer/conformance")

        assert response.status_code == 200
        html = response.text
        assert "n/a" in html

    @pytest.mark.asyncio
    async def test_oauth_status_token_ok_for_oauth(self) -> None:
        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-oauth-ok",
            url="https://oauth.example.com/mcp",
            name="OAuth Server",
            auth_type="oauth",
            oauth_token_status="ok",
            tools=[{"name": "test-tool", "inputSchema": {"type": "object", "properties": {}}}],
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-oauth-ok/conformance")

        assert response.status_code == 200
        html = response.text
        assert "token ok" in html

    @pytest.mark.asyncio
    async def test_oauth_status_token_error_for_oauth(self) -> None:
        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-oauth-error",
            url="https://oauth-error.example.com/mcp",
            name="OAuth Error Server",
            auth_type="oauth",
            oauth_token_status="error",
            tools=[{"name": "test-tool", "inputSchema": {"type": "object", "properties": {}}}],
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-oauth-error/conformance")

        assert response.status_code == 200
        html = response.text
        assert "token error" in html

    @pytest.mark.asyncio
    async def test_oauth_status_unchecked_for_oauth_empty_status(self) -> None:
        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-oauth-unchecked",
            url="https://oauth-unchecked.example.com/mcp",
            name="OAuth Unchecked Server",
            auth_type="oauth",
            oauth_token_status="",
            tools=[{"name": "test-tool", "inputSchema": {"type": "object", "properties": {}}}],
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-oauth-unchecked/conformance")

        assert response.status_code == 200
        html = response.text
        assert "unchecked" in html

    @pytest.mark.asyncio
    async def test_health_support_supports_health(self) -> None:
        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-health-true",
            url="https://health-true.example.com/mcp",
            name="Health True Server",
            supports_health_endpoint=True,
            tools=[{"name": "test-tool", "inputSchema": {"type": "object", "properties": {}}}],
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-health-true/conformance")

        assert response.status_code == 200
        html = response.text
        assert "Health Support" in html
        assert "✓ Supported" in html

    @pytest.mark.asyncio
    async def test_health_support_no_health_endpoint(self) -> None:
        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-health-false",
            url="https://health-false.example.com/mcp",
            name="Health False Server",
            supports_health_endpoint=False,
            tools=[{"name": "test-tool", "inputSchema": {"type": "object", "properties": {}}}],
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-health-false/conformance")

        assert response.status_code == 200
        html = response.text
        assert "Health Support" in html
        assert "✗ Not Supported" in html

    @pytest.mark.asyncio
    async def test_health_support_unknown_when_none(self) -> None:
        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-health-none",
            url="https://health-none.example.com/mcp",
            name="Health None Server",
            supports_health_endpoint=None,
            tools=[{"name": "test-tool", "inputSchema": {"type": "object", "properties": {}}}],
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-health-none/conformance")

        assert response.status_code == 200
        html = response.text
        assert "Health Support" in html
        assert "? Unknown" in html

    @pytest.mark.asyncio
    async def test_tool_name_issues_detected(self) -> None:
        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        tools = [
            {"name": "valid-tool", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "tool with spaces", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "", "inputSchema": {"type": "object", "properties": {}}},
        ]
        server = RegisteredServer(
            id="server-tool-issues",
            url="https://tool-issues.example.com/mcp",
            name="Tool Issues Server",
            tools=tools,
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-tool-issues/conformance")

        assert response.status_code == 200
        html = response.text
        assert "tool with spaces" in html
        assert "contains spaces" in html
        assert "unnamed tool" in html

    @pytest.mark.asyncio
    async def test_schema_conformant_displayed(self) -> None:
        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-schema-conformant",
            url="https://schema.example.com/mcp",
            name="Schema Server",
            schema_conformant=True,
            schema_issues=[],
            tools=[{"name": "test-tool", "inputSchema": {"type": "object", "properties": {}}}],
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-schema-conformant/conformance")

        assert response.status_code == 200
        html = response.text
        assert "Valid" in html

    @pytest.mark.asyncio
    async def test_schema_issues_displayed(self) -> None:
        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-schema-issues",
            url="https://schema-issues.example.com/mcp",
            name="Schema Issues Server",
            schema_conformant=False,
            schema_issues=["Invalid type", "Missing property"],
            tools=[{"name": "test-tool", "inputSchema": {"type": "object", "properties": {}}}],
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-schema-issues/conformance")

        assert response.status_code == 200
        html = response.text
        assert "Invalid type" in html
        assert "Missing property" in html

    @pytest.mark.asyncio
    async def test_resource_counts_displayed(self) -> None:
        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-counts",
            url="https://counts.example.com/mcp",
            name="Counts Server",
            tools=[{"name": "tool1", "inputSchema": {"type": "object", "properties": {}}}],
            prompts=[{"name": "prompt1"}],
            resources=[{"name": "resource1", "uri": "test://r1"}],
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-counts/conformance")

        assert response.status_code == 200
        html = response.text
        assert ">1</div>" in html or ">1<" in html

    @pytest.mark.asyncio
    async def test_discovery_triggered_when_capabilities_empty(self) -> None:
        from unittest.mock import AsyncMock

        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        async def mock_discover(server: RegisteredServer, *, timeout: float = 30.0) -> None:
            server.tools = [
                {"name": "discovered-tool", "inputSchema": {"type": "object", "properties": {}}}
            ]
            server.prompts = []
            server.resources = []
            await app.state.registry.register(server)

        app.state.discovery_service.discover_immediately = mock_discover  # type: ignore[method-assign]

        server = RegisteredServer(
            id="server-discover",
            url="https://discover.example.com/mcp",
            name="Discover Server",
            tools=[],
            prompts=[],
            resources=[],
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-discover/conformance")

        assert response.status_code == 200
        html = response.text
        assert "Tools" in html
        assert ">1<" in html or ">1 </div>" in html

    @pytest.mark.asyncio
    async def test_discovery_failure_handled_gracefully(self) -> None:
        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        async def mock_discover_fail(server: RegisteredServer, *, timeout: float = 30.0) -> None:
            raise RuntimeError("Connection failed")

        app.state.discovery_service.discover_immediately = mock_discover_fail  # type: ignore[method-assign]

        server = RegisteredServer(
            id="server-discovery-fail",
            url="https://discovery-fail.example.com/mcp",
            name="Discovery Fail Server",
            tools=[],
            prompts=[],
            resources=[],
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-discovery-fail/conformance")

        assert response.status_code == 200


class TestOfficialConformanceMissingDeps:
    def test_returns_empty_when_both_exist(self, monkeypatch):
        from devhub.routes import ui_conformance

        def mock_which(exe):
            return f"/usr/bin/{exe}"

        monkeypatch.setattr(ui_conformance.shutil, "which", mock_which)

        result = ui_conformance.official_conformance_missing_deps()

        assert result == []

    def test_returns_node_when_missing(self, monkeypatch):
        from devhub.routes import ui_conformance

        def mock_which(exe):
            if exe == "npx":
                return "/usr/bin/npx"
            return None

        monkeypatch.setattr(ui_conformance.shutil, "which", mock_which)

        result = ui_conformance.official_conformance_missing_deps()

        assert result == ["node"]

    def test_returns_npx_when_missing(self, monkeypatch):
        from devhub.routes import ui_conformance

        def mock_which(exe):
            if exe == "node":
                return "/usr/bin/node"
            return None

        monkeypatch.setattr(ui_conformance.shutil, "which", mock_which)

        result = ui_conformance.official_conformance_missing_deps()

        assert result == ["npx"]

    def test_returns_both_when_missing(self, monkeypatch):
        from devhub.routes import ui_conformance

        def mock_which(exe):
            return None

        monkeypatch.setattr(ui_conformance.shutil, "which", mock_which)

        result = ui_conformance.official_conformance_missing_deps()

        assert set(result) == {"node", "npx"}


class TestGetServerConformanceOfficialStatus:
    def test_returns_404_when_server_not_found(self):
        from fastapi.testclient import TestClient
        from devhub.app import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/ui/server/nonexistent-id/conformance/official/status")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_available_when_dependencies_exist(self, monkeypatch):
        import shutil as shutil_module

        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        def mock_which(exe):
            return f"/usr/bin/{exe}"

        monkeypatch.setattr(shutil_module, "which", mock_which)

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-available",
            url="https://example.com/mcp",
            name="Test Server",
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-available/conformance/official/status")

        assert response.status_code == 200
        html = response.text
        assert "Run" in html
        assert "Missing Dependencies" not in html

    @pytest.mark.asyncio
    async def test_shows_missing_node(self, monkeypatch):
        import shutil as shutil_module

        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        def mock_which(exe):
            if exe == "npx":
                return "/usr/bin/npx"
            return None

        monkeypatch.setattr(shutil_module, "which", mock_which)

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-missing-node",
            url="https://example.com/mcp",
            name="Test Server",
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-missing-node/conformance/official/status")

        assert response.status_code == 200
        html = response.text
        assert "Missing Dependencies" in html
        assert "node" in html
        assert 'hx-post="/ui/server/server-missing-node/conformance/official/run"' not in html

    @pytest.mark.asyncio
    async def test_passes_target_url_to_template(self, monkeypatch):
        import shutil as shutil_module

        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        def mock_which(exe):
            return f"/usr/bin/{exe}"

        monkeypatch.setattr(shutil_module, "which", mock_which)

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-url-test",
            url="https://test-server.example.com/mcp",
            name="URL Test Server",
        )
        await app.state.registry.register(server)

        response = client.get("/ui/server/server-url-test/conformance/official/status")

        assert response.status_code == 200
        html = response.text
        assert "https://test-server.example.com/mcp" in html


class TestRunServerConformanceOfficial:
    def test_returns_404_when_server_not_found(self):
        from fastapi.testclient import TestClient
        from devhub.app import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/ui/server/nonexistent-id/conformance/official/run")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_with_missing_deps_renders_available_false(self, monkeypatch):
        import shutil as shutil_module

        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        def mock_which(exe):
            if exe == "npx":
                return "/usr/bin/npx"
            return None

        monkeypatch.setattr(shutil_module, "which", mock_which)

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-missing-deps",
            url="https://example.com/mcp",
            name="Test Server",
        )
        await app.state.registry.register(server)

        response = client.post("/ui/server/server-missing-deps/conformance/official/run")

        assert response.status_code == 200
        html = response.text
        assert "Missing Dependencies" in html
        assert "node" in html

    @pytest.mark.asyncio
    async def test_run_renders_command_output_and_totals(self, monkeypatch):
        import shutil as shutil_module

        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        def mock_which(exe):
            return f"/usr/bin/{exe}"

        monkeypatch.setattr(shutil_module, "which", mock_which)

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-run-test",
            url="https://run-test.example.com/mcp",
            name="Run Test Server",
        )
        await app.state.registry.register(server)

        response = client.post("/ui/server/server-run-test/conformance/official/run")

        assert response.status_code == 200
        html = response.text
        assert "MCP_CONFORMANCE_TARGET=" in html
        assert "sh " in html
        assert "Run Results" in html
        assert "Exit Code" in html
        assert "Raw Output" in html

    @pytest.mark.asyncio
    async def test_run_with_scenario_filter(self, monkeypatch):
        import shutil as shutil_module

        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        def mock_which(exe):
            return f"/usr/bin/{exe}"

        monkeypatch.setattr(shutil_module, "which", mock_which)

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-filter-test",
            url="https://filter-test.example.com/mcp",
            name="Filter Test Server",
        )
        await app.state.registry.register(server)

        response = client.post(
            "/ui/server/server-filter-test/conformance/official/run",
            params={"scenario": "Initialize"},
        )

        assert response.status_code == 200
        html = response.text
        assert "--filter Initialize" in html

    @pytest.mark.asyncio
    async def test_temp_file_deleted_after_run(self, monkeypatch):
        import os
        import shutil as shutil_module

        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        def mock_which(exe):
            return f"/usr/bin/{exe}"

        monkeypatch.setattr(shutil_module, "which", mock_which)

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-cleanup-test",
            url="https://cleanup-test.example.com/mcp",
            name="Cleanup Test Server",
        )
        await app.state.registry.register(server)

        response = client.post("/ui/server/server-cleanup-test/conformance/official/run")

        assert response.status_code == 200
        tmp_files = [f for f in os.listdir("/tmp") if f.endswith(".sh")]
        for f in tmp_files:
            os.unlink(f"/tmp/{f}")


class TestRetestServerConformanceOfficial:
    def test_returns_404_when_server_not_found(self):
        from fastapi.testclient import TestClient
        from devhub.app import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/server/nonexistent-id/conformance/official/retest",
            params={"scenario": "Initialize"},
        )

        assert response.status_code == 404

    def test_returns_400_when_scenario_param_empty(self):
        from fastapi.testclient import TestClient
        from devhub.app import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/ui/server/some-server/conformance/official/retest")

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_returns_400_when_missing_deps(self, monkeypatch):
        import shutil as shutil_module

        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        def mock_which(exe):
            if exe == "npx":
                return "/usr/bin/npx"
            return None

        monkeypatch.setattr(shutil_module, "which", mock_which)

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-missing-deps",
            url="https://example.com/mcp",
            name="Test Server",
        )
        await app.state.registry.register(server)

        response = client.post(
            "/ui/server/server-missing-deps/conformance/official/retest",
            params={"scenario": "Initialize"},
        )

        assert response.status_code == 400
        assert "Missing dependencies" in response.text

    @pytest.mark.asyncio
    async def test_passing_scenario_returns_two_oob_fragments(self, monkeypatch):
        import shutil as shutil_module

        from unittest.mock import AsyncMock, patch

        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        def mock_which(exe):
            return f"/usr/bin/{exe}"

        monkeypatch.setattr(shutil_module, "which", mock_which)

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-passing",
            url="https://passing.example.com/mcp",
            name="Passing Server",
        )
        await app.state.registry.register(server)

        output = """=== SUMMARY ===
✓ Initialize: 5 passed, 0 failed
Total: 5 passed, 0 failed
"""

        async def mock_subprocess_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(output.encode("utf-8"), b""))
            mock_proc.returncode = 0
            return mock_proc

        with patch("asyncio.create_subprocess_exec", mock_subprocess_exec):
            response = client.post(
                "/ui/server/server-passing/conformance/official/retest",
                params={"scenario": "Initialize"},
            )

        assert response.status_code == 200
        html = response.text
        assert 'hx-swap-oob="delete"' in html
        assert 'hx-swap-oob="beforeend:#official-passed-server-passing"' in html

    @pytest.mark.asyncio
    async def test_failing_scenario_returns_single_fragment(self, monkeypatch):
        import shutil as shutil_module

        from unittest.mock import AsyncMock, patch

        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        def mock_which(exe):
            return f"/usr/bin/{exe}"

        monkeypatch.setattr(shutil_module, "which", mock_which)

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-failing",
            url="https://failing.example.com/mcp",
            name="Failing Server",
        )
        await app.state.registry.register(server)

        output = """=== SUMMARY ===
✗ Tool Calls: 2 passed, 3 failed
Total: 2 passed, 3 failed
"""

        async def mock_subprocess_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(output.encode("utf-8"), b""))
            mock_proc.returncode = 1
            return mock_proc

        with patch("asyncio.create_subprocess_exec", mock_subprocess_exec):
            response = client.post(
                "/ui/server/server-failing/conformance/official/retest",
                params={"scenario": "Tool Calls"},
            )

        assert response.status_code == 200
        html = response.text
        assert 'hx-swap-oob="delete"' not in html
        assert "beforeend:#official-passed" not in html

    @pytest.mark.asyncio
    async def test_scenario_not_in_output_creates_placeholder(self, monkeypatch):
        import shutil as shutil_module

        from unittest.mock import AsyncMock, patch

        from fastapi.testclient import TestClient
        from devhub.app import create_app
        from devhub.models.server import RegisteredServer

        def mock_which(exe):
            return f"/usr/bin/{exe}"

        monkeypatch.setattr(shutil_module, "which", mock_which)

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        server = RegisteredServer(
            id="server-unknown",
            url="https://unknown.example.com/mcp",
            name="Unknown Server",
        )
        await app.state.registry.register(server)

        output = """=== SUMMARY ===
✓ Initialize: 5 passed, 0 failed
Total: 5 passed, 0 failed
"""

        async def mock_subprocess_exec(*args, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(output.encode("utf-8"), b""))
            mock_proc.returncode = 0
            return mock_proc

        with patch("asyncio.create_subprocess_exec", mock_subprocess_exec):
            response = client.post(
                "/ui/server/server-unknown/conformance/official/retest",
                params={"scenario": "Unknown Scenario"},
            )

        assert response.status_code == 200
        html = response.text
        assert "Unknown Scenario" in html
        assert 'hx-swap-oob="delete"' in html
        assert 'hx-swap-oob="beforeend:#official-passed-server-unknown"' in html
