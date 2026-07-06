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
        assert "set -euo pipefail" in result

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

    def test_has_curl_with_status_function(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "curl_with_status() {" in result
        assert 'curl "${curl_args[@]}"' in result

    def test_has_cleanup_trap(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "trap cleanup EXIT" in result

    def test_has_required_http_headers(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "Content-Type: application/json" in result
        assert "Accept: application/json, text/event-stream" in result
        assert "MCP-Protocol-Version: $PROTOCOL_VERSION" in result

    def test_has_auth_header_when_provided(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "Authorization: Bearer $AUTH_HEADER_VALUE" in result

    def test_has_mcp_hub_auth_logic(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert "MCPHUB_USER" in result
        assert "MCPHUB_PASS" in result
        assert "prompt_mcp_hub_auth" in result

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
        assert "Calling tool: $TOOL_NAME" in result

    def test_has_401_retry_logic(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.sh.j2")
        result = template.render(**sample_context)
        assert 'status" == "401"' in result
        assert "Unauthorized" in result
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
            "init_body": '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}},"id":1}',
            "call_body": '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"echo","arguments":{"text":"hello"}},"id":2}',
            "auth_header_value": "Bearer test-token-123",
            "protocol_version": "2024-11-05",
            "tool_name": "echo",
            "is_hub": "True",
            "target_server_id": "server-456",
            "ca_bundle": "",
            "is_streamable": True,
        }

    def test_renders_without_errors(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert result is not None
        assert len(result) > 0

    def test_compiles_without_errors(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        try:
            compile(result, "<template>", "exec")
        except SyntaxError as e:
            pytest.fail(f"Template compilation failed: {e}")

    def test_passes_py_compile(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(result)
            f.flush()
            try:
                import py_compile

                py_compile.compile(f.name, doraise=True)
            except py_compile.PyCompileError as e:
                pytest.fail(f"py_compile failed: {e}")

    def test_all_variables_substituted(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "{{ base_url }}" not in result
        assert "{{ init_body }}" not in result
        assert "{{ call_body }}" not in result
        assert "{{ auth_header_value }}" not in result
        assert "{{ protocol_version }}" not in result
        assert "{{ tool_name }}" not in result
        assert "{{ is_hub }}" not in result
        assert "{{ target_server_id }}" not in result

    def test_has_shebang(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert result.startswith("#!/usr/bin/env python3")

    def test_has_docstring(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "MCP tool invocation script (direct or via Dev Hub)" in result

    def test_streamable_docstring_text(self, jinja_env: Environment, sample_context: dict) -> None:
        sample_context["is_streamable"] = True
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "Uses direct HTTP for streamable servers (no SDK required)" in result

    def test_non_streamable_docstring_text(self, jinja_env: Environment) -> None:
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
            "is_streamable": False,
        }
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**context)
        assert "Requires: mcp (Python SDK). Install: pip install mcp" in result

    def test_is_streamable_true_sets_constant(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        sample_context["is_streamable"] = True
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "IS_STREAMABLE = True" in result

    def test_is_streamable_false_sets_constant(self, jinja_env: Environment) -> None:
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
            "is_streamable": False,
        }
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**context)
        assert "IS_STREAMABLE = False" in result

    def test_is_hub_true_sets_target_server(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert 'TARGET_SERVER = "server-456"' in result

    def test_is_hub_false_sets_target_server_none(self, jinja_env: Environment) -> None:
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
            "is_streamable": True,
        }
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**context)
        assert "TARGET_SERVER = None" in result

    def test_has_required_imports(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "import asyncio" in result
        assert "import base64" in result
        assert "import getpass" in result
        assert "import json" in result
        assert "import os" in result
        assert "import sys" in result
        assert "from typing import Optional" in result
        assert "import httpx" in result

    def test_has_prompt_mcp_hub_basic_auth_function(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "def prompt_mcp_hub_basic_auth() -> Optional[str]:" in result

    def test_has_build_headers_function(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert (
            "def build_headers(auth_header: Optional[str], target_server: Optional[str]) -> dict[str, str]:"
            in result
        )

    def test_has_parse_sse_body_function(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "def parse_sse_body(response: httpx.Response) -> str:" in result

    def test_has_run_streamable_function(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "async def run_streamable() -> None:" in result

    def test_has_run_via_sdk_function(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "async def run_via_sdk() -> None:" in result

    def test_has_main_entry_point(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "async def main() -> None:" in result
        assert 'if __name__ == "__main__":' in result
        assert "asyncio.run(main())" in result

    def test_has_protocol_version_constant(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert 'PROTOCOL_VERSION = "2024-11-05"' in result

    def test_has_base_url_constant(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert 'BASE_URL = "http://localhost:8000/mcp"' in result

    def test_has_auth_header_value_constant(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert 'AUTH_HEADER_ENV = "Bearer test-token-123"' in result

    def test_has_tool_name_constant(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert 'TOOL_NAME = "echo"' in result

    def test_has_init_body_json_loaded(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "INIT_BODY = json.loads(" in result

    def test_has_call_body_json_loaded(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "CALL_BODY = json.loads(" in result

    def test_has_sse_handling_in_streamable(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "text/event-stream" in result
        assert "data:" in result

    def test_has_401_retry_logic(self, jinja_env: Environment, sample_context: dict) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "status_code == 401" in result
        assert "Unauthorized" in result
        assert "sys.exit(1)" in result

    def test_has_initialize_notification_steps(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "Initializing MCP session" in result
        assert "Sending MCP initialized notification" in result
        assert "Calling tool:" in result

    def test_has_mcp_protocol_version_header(
        self, jinja_env: Environment, sample_context: dict
    ) -> None:
        template = jinja_env.get_template("tool_script.py.j2")
        result = template.render(**sample_context)
        assert "MCP-Protocol-Version" in result
