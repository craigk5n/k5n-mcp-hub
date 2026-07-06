from __future__ import annotations

import asyncio
import os
import signal
import socket
import tempfile
import threading
import time
from unittest import mock

import httpx
import pytest
import yaml

from mcp_hub.__main__ import main, parse_args, resolve_settings
from mcp_hub.app import create_app, register_background_task


class TestParseArgs:
    def test_parse_defaults(self) -> None:
        args = parse_args([])
        assert args.config is None
        assert args.host is None
        assert args.port is None

    def test_parse_config_option(self) -> None:
        args = parse_args(["--config", "/path/to/config.yaml"])
        assert args.config == "/path/to/config.yaml"
        assert args.host is None
        assert args.port is None

    def test_parse_host_option(self) -> None:
        args = parse_args(["--host", "0.0.0.0"])
        assert args.config is None
        assert args.host == "0.0.0.0"
        assert args.port is None

    def test_parse_port_option(self) -> None:
        args = parse_args(["--port", "8765"])
        assert args.config is None
        assert args.host is None
        assert args.port == 8765

    def test_parse_both_options(self) -> None:
        args = parse_args(["--config", "/path/to/config.yaml", "--port", "9000"])
        assert args.config == "/path/to/config.yaml"
        assert args.port == 9000

    def test_parse_all_options(self) -> None:
        args = parse_args(
            ["--config", "/path/to/config.yaml", "--host", "0.0.0.0", "--port", "9000"]
        )
        assert args.config == "/path/to/config.yaml"
        assert args.host == "0.0.0.0"
        assert args.port == 9000


class TestResolveSettings:
    def test_resolve_settings_default_host_and_port(self) -> None:
        settings = resolve_settings(None, None, None)
        assert settings.server.http_host == "127.0.0.1"
        assert settings.server.http_port == 8080

    def test_resolve_settings_host_override(self) -> None:
        settings = resolve_settings(None, "0.0.0.0", None)
        assert settings.server.http_host == "0.0.0.0"

    def test_resolve_settings_port_override(self) -> None:
        settings = resolve_settings(None, None, 8765)
        assert settings.server.http_port == 8765

    def test_resolve_settings_config_file(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({"server": {"http_host": "0.0.0.0", "http_port": 9000}}, f)
            config_path = f.name

        try:
            settings = resolve_settings(config_path, None, None)
            assert settings.server.http_host == "0.0.0.0"
            assert settings.server.http_port == 9000
        finally:
            os.unlink(config_path)

    def test_resolve_settings_config_and_overrides(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({"server": {"http_host": "0.0.0.0", "http_port": 9000}}, f)
            config_path = f.name

        try:
            settings = resolve_settings(config_path, "192.168.1.1", 8765)
            assert settings.server.http_host == "192.168.1.1"
            assert settings.server.http_port == 8765
        finally:
            os.unlink(config_path)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_server(port: int, timeout: float = 10.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with httpx.Client(f"http://127.0.0.1:{port}", timeout=1.0) as client:
                response = client.get("/healthz")
                if response.status_code == 200:
                    return True
        except (httpx.ConnectError, httpx.ReadTimeout):
            time.sleep(0.1)
    return False


class TestServerStartup:
    def test_server_starts_on_specified_port(self) -> None:
        port = find_free_port()

        main_module = pytest.importorskip("mcp_hub.__main__")
        with mock.patch.object(main_module, "uvicorn") as mock_uvicorn:
            mock_uvicorn.run = mock.MagicMock(side_effect=KeyboardInterrupt())
            thread = threading.Thread(
                target=main_module.main, args=(["--port", str(port)],), daemon=True
            )
            thread.start()
            time.sleep(0.5)
            mock_uvicorn.run.assert_called_once()
            call_kwargs = mock_uvicorn.run.call_args.kwargs
            assert call_kwargs["port"] == port


class TestGracefulShutdown:
    def test_graceful_shutdown_no_traceback(self) -> None:
        import sys

        main_module = pytest.importorskip("mcp_hub.__main__")

        with mock.patch.object(main_module, "uvicorn") as mock_uvicorn:
            mock_uvicorn.run = mock.MagicMock(side_effect=KeyboardInterrupt())
            try:
                main_module.main(["--port", "8765"])
            except KeyboardInterrupt:
                pass

            mock_uvicorn.run.assert_called_once()


class TestBackgroundTaskCancellation:
    @pytest.mark.asyncio
    async def test_background_task_cancelled_on_shutdown(self) -> None:
        task_cancelled = asyncio.Event()

        async def background_task() -> None:
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                task_cancelled.set()
                raise

        app = create_app()

        from mcp_hub.app import AppContext, lifespan

        app.state.context = AppContext(background_tasks=[])

        bg_task = asyncio.create_task(background_task())
        register_background_task(app, bg_task)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/healthz")

        async with lifespan(app):
            pass

        assert task_cancelled.is_set()


class TestUvicornIntegration:
    def test_uvicorn_receives_correct_arguments(self) -> None:
        main_module = pytest.importorskip("mcp_hub.__main__")

        with mock.patch.object(main_module, "uvicorn") as mock_uvicorn:
            mock_uvicorn.run = mock.MagicMock(side_effect=KeyboardInterrupt())
            try:
                main_module.main(["--port", "8765"])
            except KeyboardInterrupt:
                pass

            mock_uvicorn.run.assert_called_once()
            call_args = mock_uvicorn.run.call_args
            assert call_args.args[0] == "mcp_hub.app:create_app"
            assert call_args.kwargs["host"] == "127.0.0.1"
            assert call_args.kwargs["port"] == 8765
            assert call_args.kwargs["factory"] is True
            assert call_args.kwargs["lifespan"] == "on"

    def test_uvicorn_receives_host_override(self) -> None:
        main_module = pytest.importorskip("mcp_hub.__main__")

        with mock.patch.object(main_module, "uvicorn") as mock_uvicorn:
            mock_uvicorn.run = mock.MagicMock(side_effect=KeyboardInterrupt())
            try:
                main_module.main(["--host", "0.0.0.0", "--port", "8765"])
            except KeyboardInterrupt:
                pass

            mock_uvicorn.run.assert_called_once()
            call_args = mock_uvicorn.run.call_args
            assert call_args.kwargs["host"] == "0.0.0.0"
            assert call_args.kwargs["port"] == 8765
