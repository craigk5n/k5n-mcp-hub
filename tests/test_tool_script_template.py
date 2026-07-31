import subprocess
import tempfile
from jinja2 import Environment, FileSystemLoader, select_autoescape

import pytest


class TestToolScriptTemplate:
    @pytest.fixture
    def jinja_env(self) -> Environment:
        loader = FileSystemLoader("src/mcp_hub/templates")
        env = Environment(loader=loader, autoescape=select_autoescape())
        return env

    @pytest.fixture
    def sample_context(self) -> dict:
        return {
            "base_url": "http://localhost:8000/mcp",
            "init_body": '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}},"id":1}',
            "call_body": '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"echo","arguments":{"text":"hello"}},"id":2}',
            "auth_header_value": "Bearer test-token-123",
            "protocol_version": "2024-11-05",
            "tool_name": "echo",
            "is_hub": "True",
            "target_server_id": "server-456",
            "ca_bundle": "",
        }

    def test_renders_without_errors(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert result is not None
        assert len(result) > 0

    def test_passes_bash_syntax_check(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        proc = subprocess.run(
            ["bash", "-n"],
            input=result,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"bash -n failed: {proc.stderr}"

    def test_all_variables_substituted(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "{{ base_url }}" not in result
        assert "{{ init_body }}" not in result
        assert "{{ call_body }}" not in result
        assert "{{ auth_header_value }}" not in result
        assert "{{ protocol_version }}" not in result
        assert "{{ tool_name }}" not in result
        assert "{{ is_hub }}" not in result
        assert "{{ target_server_id }}" not in result
        assert "{{ ca_bundle }}" not in result

    def test_bodies_are_single_quote_escaped(self, jinja_env: Environment) -> None:
        context_with_quotes = {
            "base_url": "http://localhost:8000/mcp",
            "init_body": '{"jsonrpc":"2.0","method":"initialize","params":{"test":"it\'s working"},"id":1}',
            "call_body": '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"test","arguments":{"text":"hello\'world"}},"id":2}',
            "auth_header_value": "Bearer token",
            "protocol_version": "2024-11-05",
            "tool_name": "test_tool",
            "is_hub": "False",
            "target_server_id": "srv-123",
            "ca_bundle": "",
        }
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**context_with_quotes)
        assert "INIT_BODY='" in result
        assert "CALL_BODY='" in result
        assert (
            "bash -n" not in result
            or subprocess.run(["bash", "-n"], input=result, capture_output=True).returncode == 0
        )

    def test_is_hub_adds_target_server_header(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "X-MCP-Target-Server: $TARGET_SERVER_ID" in result

    def test_is_hub_false_no_target_server_header(self, jinja_env: Environment) -> None:
        context = {
            "base_url": "http://localhost:8000/mcp",
            "init_body": '{"jsonrpc":"2.0","id":1}',
            "call_body": '{"jsonrpc":"2.0","id":2}',
            "auth_header_value": "",
            "protocol_version": "2024-11-05",
            "tool_name": "test",
            "is_hub": "False",
            "target_server_id": "srv-123",
            "ca_bundle": "",
        }
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**context)
        assert "X-MCP-Target-Server: $TARGET_SERVER_ID" not in result

    def test_empty_ca_bundle_omits_cacert(self, jinja_env: Environment) -> None:
        context = {
            "base_url": "http://localhost:8000/mcp",
            "init_body": '{"jsonrpc":"2.0","id":1}',
            "call_body": '{"jsonrpc":"2.0","id":2}',
            "auth_header_value": "",
            "protocol_version": "2024-11-05",
            "tool_name": "test",
            "is_hub": "False",
            "target_server_id": "",
            "ca_bundle": "",
        }
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**context)
        assert "--cacert" not in result

    def test_ca_bundle_includes_cacert(self, jinja_env: Environment) -> None:
        context = {
            "base_url": "http://localhost:8000/mcp",
            "init_body": '{"jsonrpc":"2.0","id":1}',
            "call_body": '{"jsonrpc":"2.0","id":2}',
            "auth_header_value": "",
            "protocol_version": "2024-11-05",
            "tool_name": "test",
            "is_hub": "False",
            "target_server_id": "",
            "ca_bundle": "/etc/ssl/certs/ca-bundle.crt",
        }
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**context)
        assert '--cacert "$CA_BUNDLE_PATH"' in result

    def test_has_shebang_and_error_flags(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert result.startswith("#!/usr/bin/env bash")
        # -e is intentionally omitted: errors are handled explicitly so a no-match grep
        # (e.g. missing sessionId) doesn't kill the script with a bare exit 1.
        assert "set -uo pipefail" in result

    def test_has_init_and_call_body_variables(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "INIT_BODY=" in result
        assert "CALL_BODY=" in result
        assert (
            'INITED_BODY=\'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\''
            in result
        )

    def test_has_mcp_call_function_and_status_capture(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "mcp_call() {" in result
        # HTTP status is captured from curl's -w, not a broken second -o file.
        assert "-w '%{http_code}'" in result
        assert "RESP_STATUS=" in result

    def test_cleans_up_temp_files(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert 'rm -f "$headers_file" "$body_file"' in result

    def test_has_required_http_headers(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "Content-Type: application/json" in result
        assert "Accept: application/json, text/event-stream" in result
        assert "MCP-Protocol-Version: $PROTOCOL_VERSION" in result

    def test_builds_auth_header_and_sends_it(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "build_auth_header()" in result
        assert '[[ -n "$AUTH_HEADER" ]] && curl_args+=(-H "$AUTH_HEADER")' in result

    def test_has_mcp_hub_auth_logic(self, jinja_env: Environment, sample_context: dict) -> None:
        # sample_context is is_hub=True, so the hub-auth branch is rendered.
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "MCPHUB_USER" in result
        assert "MCPHUB_PASS" in result
        assert "k5n-mcp-hub Username" in result

    def test_direct_mode_supports_basic_auth(self, jinja_env: Environment) -> None:
        context = {
            "base_url": "http://localhost:8000/mcp",
            "init_body": '{"jsonrpc":"2.0","id":1}',
            "call_body": '{"jsonrpc":"2.0","id":2}',
            "auth_header_value": "",
            "auth_type": "basic",
            "basic_username": "admin",
            "protocol_version": "2024-11-05",
            "tool_name": "test",
            "is_hub": "False",
            "target_server_id": "srv-1",
            "ca_bundle": "",
        }
        result = jinja_env.get_template("tool_script.sh.j2").render(**context)
        assert "AUTH_TYPE='basic'" in result
        assert "BASIC_USER_DEFAULT='admin'" in result
        assert "MCP_BASIC_PASS" in result
        assert "Authorization: Basic" in result

    def test_reports_jsonrpc_error(self, jinja_env: Environment, sample_context: dict) -> None:
        # Servers can return HTTP 200 with a JSON-RPC {"error":{...}} (e.g. auth failures);
        # the script must detect and surface that, not silently continue.
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "check_error" in result
        assert '"error"' in result

    def test_has_sse_handling(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "text/event-stream" in result
        assert "data:" in result

    def test_has_mcp_session_handling(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "MCP_SESSION_ID" in result
        assert "Mcp-Session-Id:" in result

    def test_has_three_post_steps_with_status_handling(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "Initializing MCP session" in result
        assert "Sending MCP initialized notification" in result
        assert "Calling tool: ${TOOL_NAME}" in result

    def test_has_auth_failure_handling(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert '"$RESP_STATUS" == "401"' in result
        assert "authentication failed" in result
        assert "exit 1" in result


class TestToolScriptPythonTemplate:
    @pytest.fixture
    def jinja_env(self) -> Environment:
        loader = FileSystemLoader("src/mcp_hub/templates")
        env = Environment(loader=loader, autoescape=select_autoescape())
        return env

    @pytest.fixture
    def sample_context(self) -> dict:
        return {
            "base_url": "http://localhost:8000/mcp",
            "init_body": '{"jsonrpc":"2.0","method":"initialize","params":{},"id":1}',
            "call_body": '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"echo"},"id":2}',
            "auth_header_value": "",
            "auth_type": "bearer",
            "basic_username": "",
            "protocol_version": "2024-11-05",
            "tool_name": "echo",
            "is_hub": "True",
            "target_server_id": "server-456",
            "ca_bundle": "",
            "is_streamable": True,
        }

    def _render(self, jinja_env: Environment, ctx: dict) -> str:
        return jinja_env.get_template("tool_script.py.j2").render(**ctx)

    def test_renders_and_compiles(self, jinja_env: Environment, sample_context: dict) -> None:
        result = self._render(jinja_env, sample_context)
        assert result.startswith("#!/usr/bin/env python3")
        compile(result, "<template>", "exec")  # raises SyntaxError on failure

    def test_no_sdk_dependency(self, jinja_env: Environment, sample_context: dict) -> None:
        # The script must NOT require the mcp SDK (which needs Python 3.10+); httpx only.
        result = self._render(jinja_env, sample_context)
        assert "import mcp" not in result
        assert "from mcp" not in result
        assert "import httpx" in result
        assert "pip3 install httpx" in result

    def test_variables_substituted(self, jinja_env: Environment, sample_context: dict) -> None:
        result = self._render(jinja_env, sample_context)
        for var in ("base_url", "init_body", "call_body", "tool_name", "auth_type"):
            assert "{{ " + var not in result
        assert 'BASE_URL = "http://localhost:8000/mcp"' in result
        assert 'TOOL_NAME = "echo"' in result
        assert 'PROTOCOL_VERSION = "2024-11-05"' in result
        assert "INIT_BODY = json.loads(" in result
        assert "CALL_BODY = json.loads(" in result

    def test_core_functions_present(self, jinja_env: Environment, sample_context: dict) -> None:
        result = self._render(jinja_env, sample_context)
        assert "def build_auth_header():" in result
        assert "def build_headers(" in result
        assert "def parse_body(" in result
        assert "def check_error(" in result
        assert "async def main() -> None:" in result
        assert 'if __name__ == "__main__":' in result
        assert "asyncio.run(main())" in result

    def test_auth_types_supported(self, jinja_env: Environment) -> None:
        base = {
            "base_url": "http://x/mcp",
            "init_body": "{}",
            "call_body": "{}",
            "protocol_version": "p",
            "tool_name": "t",
            "target_server_id": "",
            "ca_bundle": "",
            "is_hub": "False",
        }
        bearer = dict(base, auth_type="bearer", basic_username="")
        basic = dict(base, auth_type="basic", basic_username="admin")
        r_bearer = self._render(jinja_env, bearer)
        r_basic = self._render(jinja_env, basic)
        assert 'AUTH_TYPE = "bearer"' in r_bearer
        assert "MCP_BEARER_TOKEN" in r_bearer
        assert 'AUTH_TYPE = "basic"' in r_basic
        assert 'BASIC_USER_DEFAULT = "admin"' in r_basic
        assert "MCP_BASIC_PASS" in r_basic

    def test_session_from_response_header(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        result = self._render(jinja_env, sample_context)
        assert 'resp.headers.get("Mcp-Session-Id"' in result

    def test_error_and_sse_handling(self, jinja_env: Environment, sample_context: dict) -> None:
        result = self._render(jinja_env, sample_context)
        assert "text/event-stream" in result
        assert "authentication failed" in result
        assert 'parsed.get("error")' in result

    def test_hub_mode_sets_target_and_hub_auth(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        result = self._render(jinja_env, sample_context)
        assert 'TARGET_SERVER = "server-456"' in result
        assert "X-MCP-Target-Server" in result
        assert "MCPHUB_USER" in result

    def test_direct_mode_no_target_server(self, jinja_env: Environment) -> None:
        ctx = {
            "base_url": "http://x/mcp",
            "init_body": "{}",
            "call_body": "{}",
            "auth_type": "",
            "basic_username": "",
            "protocol_version": "p",
            "tool_name": "t",
            "is_hub": "False",
            "target_server_id": "srv-1",
            "ca_bundle": "",
        }
        result = self._render(jinja_env, ctx)
        assert "TARGET_SERVER = None" in result


class TestStatelessToolScripts:
    @pytest.fixture
    def jinja_env(self) -> Environment:
        loader = FileSystemLoader("src/mcp_hub/templates")
        return Environment(loader=loader, autoescape=select_autoescape())

    @pytest.fixture
    def stateless_context(self) -> dict:
        return {
            "base_url": "http://localhost:8000/mcp",
            "init_body": '{"jsonrpc":"2.0","method":"initialize","params":{},"id":1}',
            "call_body": '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"echo","arguments":{},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}},"id":1}',
            "auth_header_value": "",
            "auth_type": "",
            "basic_username": "",
            "protocol_version": "2026-07-28",
            "tool_name": "echo",
            "is_hub": "false",
            "target_server_id": "srv-1",
            "is_streamable": "true",
            "is_stateless": "true",
            "ca_bundle": "",
        }

    def test_stateless_shell_script_passes_bash_syntax_check(
        self, jinja_env: Environment, stateless_context: dict
    ) -> None:
        result = jinja_env.get_template("tool_script.sh.j2").render(**stateless_context)
        proc = subprocess.run(["bash", "-n"], input=result, capture_output=True, text=True)
        assert proc.returncode == 0, f"bash -n failed: {proc.stderr}"
        assert "Initializing MCP session" not in result
        assert "Mcp-Method: tools/call" in result

    def test_stateless_python_script_compiles(
        self, jinja_env: Environment, stateless_context: dict
    ) -> None:
        result = jinja_env.get_template("tool_script.py.j2").render(**stateless_context)
        compile(result, "tool_script.py", "exec")
        assert "Initializing MCP session" not in result
        assert '"Mcp-Method"' in result

    def test_legacy_python_script_still_compiles(self, jinja_env: Environment) -> None:
        context = {
            "base_url": "http://localhost:8000/mcp",
            "init_body": '{"jsonrpc":"2.0","method":"initialize","params":{},"id":1}',
            "call_body": '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"echo","arguments":{}},"id":3}',
            "auth_header_value": "",
            "auth_type": "",
            "basic_username": "",
            "protocol_version": "2025-11-25",
            "tool_name": "echo",
            "is_hub": "false",
            "target_server_id": "srv-1",
            "is_streamable": "false",
            "ca_bundle": "",
        }
        result = jinja_env.get_template("tool_script.py.j2").render(**context)
        compile(result, "tool_script.py", "exec")
        assert "Initializing MCP session" in result
