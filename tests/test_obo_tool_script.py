"""Generated client scripts for on-behalf-of servers (Story 7.3).

A script can't carry an OBO credential: the token is the live caller's, and the
exchange happens in the hub. So the script has to ask for the caller's own token and
say why -- otherwise it silently produces a 401 the user can't diagnose.
"""

from __future__ import annotations

import subprocess

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


@pytest.fixture
def env() -> Environment:
    return Environment(
        loader=FileSystemLoader("src/mcp_hub/templates"), autoescape=select_autoescape()
    )


def context(**overrides) -> dict:
    base = {
        "base_url": "http://localhost:8000/mcp",
        "init_body": '{"jsonrpc":"2.0","method":"initialize","id":1}',
        "call_body": '{"jsonrpc":"2.0","method":"tools/call","id":2}',
        "auth_header_value": "",
        "protocol_version": "2025-11-25",
        "tool_name": "echo",
        "is_hub": "False",
        "target_server_id": "files",
        "ca_bundle": "",
        "auth_type": "obo",
    }
    base.update(overrides)
    return base


class TestPythonScript:
    def test_obo_reads_the_callers_own_token_from_the_environment(self, env: Environment) -> None:
        script = env.get_template("tool_script.py.j2").render(**context())

        assert 'AUTH_TYPE == "obo"' in script
        assert "MCP_USER_TOKEN" in script

    def test_obo_explains_who_the_token_belongs_to(self, env: Environment) -> None:
        script = env.get_template("tool_script.py.j2").render(**context())

        assert "exchange" in script.lower()

    def test_obo_script_is_valid_python(self, env: Environment) -> None:
        script = env.get_template("tool_script.py.j2").render(**context())

        compile(script, "tool_script.py", "exec")

    def test_no_credential_is_baked_into_the_script(self, env: Environment) -> None:
        script = env.get_template("tool_script.py.j2").render(
            **context(auth_header_value="Bearer leaked-token")
        )

        assert "leaked-token" not in script

    def test_non_obo_scripts_gain_nothing(self, env: Environment) -> None:
        script = env.get_template("tool_script.py.j2").render(**context(auth_type="bearer"))

        assert "MCP_USER_TOKEN" not in script


class TestShellScript:
    def test_obo_reads_the_callers_own_token_from_the_environment(self, env: Environment) -> None:
        script = env.get_template("tool_script.sh.j2").render(**context())

        assert "MCP_USER_TOKEN" in script

    def test_obo_script_passes_bash_syntax_check(self, env: Environment) -> None:
        script = env.get_template("tool_script.sh.j2").render(**context())

        proc = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)

        assert proc.returncode == 0, proc.stderr

    def test_non_obo_scripts_gain_nothing(self, env: Environment) -> None:
        script = env.get_template("tool_script.sh.j2").render(**context(auth_type="bearer"))

        assert "MCP_USER_TOKEN" not in script


class TestDirectModeWarning:
    @pytest.mark.parametrize("template", ["tool_script.py.j2", "tool_script.sh.j2"])
    def test_direct_mode_says_the_hub_must_do_the_exchange(
        self, env: Environment, template: str
    ) -> None:
        # Pointed straight at the backend, the caller's token has the wrong audience
        # and will be refused -- the hub is what makes it usable.
        script = env.get_template(template).render(**context(is_hub="False"))

        assert "through the hub" in script
