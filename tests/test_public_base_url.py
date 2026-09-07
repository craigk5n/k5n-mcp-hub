"""`server.public_base_url` — the address clients actually reach the hub on.

The hub cannot observe its own reachability. Inside a container it binds 0.0.0.0:8080
while the port may be published as `-p 127.0.0.1:3001:8080`; behind a reverse proxy it
may be `https://hub.example.com` with no port at all. Generated tool scripts were
built from the bind address, so they told the user to POST somewhere that only exists
inside the container.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import Settings

ECHO = Path(__file__).parent / "fixtures" / "echo_stdio_server.py"


def _settings(public_base_url: str = "", **server: Any) -> Settings:
    cfg: dict[str, Any] = {"http_host": "0.0.0.0", "http_port": 8080}
    cfg.update(server)
    if public_base_url:
        cfg["public_base_url"] = public_base_url
    return Settings(
        server=cfg,
        auth={"type": "none"},
        stdio={
            "enabled": True,
            "trusted_network": True,
            "allowed_commands": {"echo": {"command": sys.executable, "args": [str(ECHO)]}},
        },
    )


def _register_and_download(settings: Settings) -> str:
    app = create_app(settings)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/register",
            content=json.dumps(
                {
                    "id": "echo",
                    "transport_kind": "stdio",
                    "stdio_command_name": "echo",
                    "registration_type": "manual",
                }
            ),
        )
        assert resp.status_code == 201, resp.text
        script = client.post("/ui/server/echo/tool-download-python/echo")
    assert script.status_code == 200, script.text
    return script.text


class TestDefaultBehaviourIsUnchanged:
    def test_empty_falls_back_to_the_bind_address(self) -> None:
        # 0.0.0.0 is not dialable, so the existing localhost substitution must survive.
        assert 'BASE_URL = "http://localhost:8080/mcp"' in _register_and_download(_settings())

    def test_default_is_empty(self) -> None:
        assert Settings.from_defaults().server.public_base_url == ""


class TestPublicBaseUrlWins:
    def test_a_published_container_port(self) -> None:
        script = _register_and_download(_settings("http://127.0.0.1:3001"))
        assert 'BASE_URL = "http://127.0.0.1:3001/mcp"' in script
        assert "8080" not in script

    def test_a_reverse_proxied_https_host(self) -> None:
        script = _register_and_download(_settings("https://hub.example.com"))
        assert 'BASE_URL = "https://hub.example.com/mcp"' in script

    def test_a_trailing_slash_does_not_double_up(self) -> None:
        script = _register_and_download(_settings("https://hub.example.com/"))
        assert 'BASE_URL = "https://hub.example.com/mcp"' in script
        assert "//mcp" not in script

    def test_a_base_path_is_preserved(self) -> None:
        # Reverse proxies commonly mount an app under a path prefix.
        script = _register_and_download(_settings("https://example.com/hub"))
        assert 'BASE_URL = "https://example.com/hub/mcp"' in script


class TestValidation:
    @pytest.mark.parametrize("bad", ["hub.example.com", "ftp://x", "/hub", "http://"])
    def test_it_must_be_an_absolute_http_url(self, bad: str) -> None:
        # Fail at startup rather than emit scripts nobody can run.
        with pytest.raises(ValueError):
            Settings(server={"public_base_url": bad})
