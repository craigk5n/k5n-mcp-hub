import os
import subprocess
import tempfile
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parent.parent / "src" / "devhub" / "scripts" / "run-mcp-conformance.sh"


@pytest.fixture
def script_path() -> Path:
    return SCRIPT_PATH


class TestMcpConformanceScript:
    def test_missing_target_exits_2_with_usage(self, script_path: Path) -> None:
        env = os.environ.copy()
        env.pop("MCP_CONFORMANCE_TARGET", None)
        env.pop("MCP_CONFORMANCE_CMD", None)
        env.pop("MCP_CONFORMANCE_NPM_DIR", None)

        result = subprocess.run(
            ["sh", str(script_path)],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 2
        assert "Set MCP_CONFORMANCE_TARGET to the target MCP base URL." in result.stderr
        assert 'MCP_CONFORMANCE_TARGET="http://localhost:8080/mcp"' in result.stderr

    def test_successful_primary_command(self, script_path: Path) -> None:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".sh") as f:
            f.write("#!/bin/sh\necho 'conformance tests passed'\n")
            stub = f.name
        os.chmod(stub, 0o755)

        try:
            env = os.environ.copy()
            env["MCP_CONFORMANCE_CMD"] = stub
            env["MCP_CONFORMANCE_TARGET"] = "http://localhost:8080/mcp"
            env.pop("MCP_CONFORMANCE_NPM_DIR", None)
            env.pop("MCP_CONFORMANCE_SUBCOMMAND", None)
            env.pop("MCP_CONFORMANCE_ARGS", None)

            result = subprocess.run(
                ["sh", str(script_path)],
                env=env,
                capture_output=True,
                text=True,
            )

            assert result.returncode == 0
            assert "Running:" in result.stdout
            assert "conformance tests passed" in result.stdout
        finally:
            os.unlink(stub)

    def test_unknown_option_triggers_legacy_retries(self, script_path: Path) -> None:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".sh") as f:
            f.write("#!/bin/sh\necho 'unknown option: --url'\nexit 1\n")
            stub = f.name
        os.chmod(stub, 0o755)

        try:
            env = os.environ.copy()
            env["MCP_CONFORMANCE_CMD"] = stub
            env["MCP_CONFORMANCE_TARGET"] = "http://localhost:8080/mcp"
            env.pop("MCP_CONFORMANCE_NPM_DIR", None)
            env.pop("MCP_CONFORMANCE_SUBCOMMAND", None)
            env.pop("MCP_CONFORMANCE_ARGS", None)

            result = subprocess.run(
                ["sh", str(script_path)],
                env=env,
                capture_output=True,
                text=True,
            )

            assert "Running:" in result.stdout
            assert "Retrying:" in result.stdout
            assert result.returncode == 1
        finally:
            os.unlink(stub)

    def test_non_unknown_error_exits_immediately(self, script_path: Path) -> None:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".sh") as f:
            f.write("#!/bin/sh\necho 'some other error'\nexit 1\n")
            stub = f.name
        os.chmod(stub, 0o755)

        try:
            env = os.environ.copy()
            env["MCP_CONFORMANCE_CMD"] = stub
            env["MCP_CONFORMANCE_TARGET"] = "http://localhost:8080/mcp"
            env.pop("MCP_CONFORMANCE_NPM_DIR", None)
            env.pop("MCP_CONFORMANCE_SUBCOMMAND", None)
            env.pop("MCP_CONFORMANCE_ARGS", None)

            result = subprocess.run(
                ["sh", str(script_path)],
                env=env,
                capture_output=True,
                text=True,
            )

            assert "Running:" in result.stdout
            assert "Retrying:" not in result.stdout
            assert result.returncode == 1
        finally:
            os.unlink(stub)

    def test_npm_dir_resolution(self, script_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            stub = Path(tmpdir) / "npm"
            stub.write_text("#!/bin/sh\necho 'npm exec working'\n")
            stub.chmod(0o755)

            env = os.environ.copy()
            env.pop("MCP_CONFORMANCE_CMD", None)
            env["MCP_CONFORMANCE_NPM_DIR"] = "/tmp/fake-npm-dir"
            env["MCP_CONFORMANCE_TARGET"] = "http://localhost:8080/mcp"
            env["PATH"] = f"{tmpdir}:{env.get('PATH', '')}"
            env.pop("MCP_CONFORMANCE_SUBCOMMAND", None)
            env.pop("MCP_CONFORMANCE_ARGS", None)

            result = subprocess.run(
                ["sh", str(script_path)],
                env=env,
                capture_output=True,
                text=True,
            )

            assert "Running:" in result.stdout
            assert "npm --prefix" in result.stdout
            assert "@modelcontextprotocol/conformance" in result.stdout

    def test_npx_fallback(self, script_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            stub = Path(tmpdir) / "npx"
            stub.write_text("#!/bin/sh\necho 'npx working'\n")
            stub.chmod(0o755)

            env = os.environ.copy()
            env.pop("MCP_CONFORMANCE_CMD", None)
            env.pop("MCP_CONFORMANCE_NPM_DIR", None)
            env["MCP_CONFORMANCE_TARGET"] = "http://localhost:8080/mcp"
            env["PATH"] = f"{tmpdir}:{env.get('PATH', '')}"
            env.pop("MCP_CONFORMANCE_SUBCOMMAND", None)
            env.pop("MCP_CONFORMANCE_ARGS", None)

            result = subprocess.run(
                ["sh", str(script_path)],
                env=env,
                capture_output=True,
                text=True,
            )

            assert "Running:" in result.stdout
            assert "npx @modelcontextprotocol/conformance" in result.stdout

    def test_custom_subcommand(self, script_path: Path) -> None:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".sh") as f:
            f.write("#!/bin/sh\necho 'custom subcommand'\n")
            stub = f.name
        os.chmod(stub, 0o755)

        try:
            env = os.environ.copy()
            env["MCP_CONFORMANCE_CMD"] = stub
            env["MCP_CONFORMANCE_TARGET"] = "http://localhost:8080/mcp"
            env["MCP_CONFORMANCE_SUBCOMMAND"] = "client"
            env.pop("MCP_CONFORMANCE_NPM_DIR", None)
            env.pop("MCP_CONFORMANCE_ARGS", None)

            result = subprocess.run(
                ["sh", str(script_path)],
                env=env,
                capture_output=True,
                text=True,
            )

            assert "Running:" in result.stdout
            assert "client --url" in result.stdout
        finally:
            os.unlink(stub)

    def test_custom_args_appended(self, script_path: Path) -> None:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".sh") as f:
            f.write("#!/bin/sh\necho 'with extra args'\n")
            stub = f.name
        os.chmod(stub, 0o755)

        try:
            env = os.environ.copy()
            env["MCP_CONFORMANCE_CMD"] = stub
            env["MCP_CONFORMANCE_TARGET"] = "http://localhost:8080/mcp"
            env.pop("MCP_CONFORMANCE_NPM_DIR", None)
            env.pop("MCP_CONFORMANCE_SUBCOMMAND", None)
            env["MCP_CONFORMANCE_ARGS"] = "--verbose --timeout 30"

            result = subprocess.run(
                ["sh", str(script_path)],
                env=env,
                capture_output=True,
                text=True,
            )

            assert "Running:" in result.stdout
            assert "--verbose --timeout 30" in result.stdout
        finally:
            os.unlink(stub)

    def test_legacy_retry_all_fail(self, script_path: Path) -> None:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".sh") as f:
            f.write("#!/bin/sh\n")
            f.write('echo "call $1"\n')
            f.write("exit 1\n")
            stub = f.name
        os.chmod(stub, 0o755)

        try:
            env = os.environ.copy()
            env["MCP_CONFORMANCE_CMD"] = stub
            env["MCP_CONFORMANCE_TARGET"] = "http://localhost:8080/mcp"
            env.pop("MCP_CONFORMANCE_NPM_DIR", None)
            env.pop("MCP_CONFORMANCE_SUBCOMMAND", None)
            env.pop("MCP_CONFORMANCE_ARGS", None)

            result = subprocess.run(
                ["sh", str(script_path)],
                env=env,
                capture_output=True,
                text=True,
            )

            assert result.returncode == 1
        finally:
            os.unlink(stub)

    def test_unknown_command_triggers_retries(self, script_path: Path) -> None:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".sh") as f:
            f.write("#!/bin/sh\necho 'unknown command: server'\nexit 1\n")
            stub = f.name
        os.chmod(stub, 0o755)

        try:
            env = os.environ.copy()
            env["MCP_CONFORMANCE_CMD"] = stub
            env["MCP_CONFORMANCE_TARGET"] = "http://localhost:8080/mcp"
            env.pop("MCP_CONFORMANCE_NPM_DIR", None)
            env.pop("MCP_CONFORMANCE_SUBCOMMAND", None)
            env.pop("MCP_CONFORMANCE_ARGS", None)

            result = subprocess.run(
                ["sh", str(script_path)],
                env=env,
                capture_output=True,
                text=True,
            )

            assert "Running:" in result.stdout
            assert "Retrying:" in result.stdout
            assert result.returncode == 1
        finally:
            os.unlink(stub)
