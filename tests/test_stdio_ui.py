"""What the admin UI says about a stdio server (Epic 9, Story 9.5).

The important one is the identity notice. A stdio server runs under a single
service identity shared by every caller (ADR 0007), and someone reading a tool list
on a hub whose headline feature is per-user identity will assume they are getting
it. Saying so on the card is the difference between a documented limitation and a
surprise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from mcp_hub.app import create_app
from mcp_hub.config import Settings
from mcp_hub.models.server import RegisteredServer
from mcp_hub.utils import dom_id

ECHO = Path(__file__).parent / "fixtures" / "echo_stdio_server.py"


def _env() -> Environment:
    templates_dir = Path(__file__).parent.parent / "src" / "mcp_hub" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["dom_id"] = dom_id
    return env


def _stdio_server() -> RegisteredServer:
    return RegisteredServer(
        id="echo",
        url="",
        transport_kind="stdio",
        stdio_command_name="echo",
        mcp_transport="stdio",
        healthy=True,
    )


class TestBadges:
    def test_transport_badge_names_stdio(self) -> None:
        html = _env().get_template("_health_badge.html").render(server=_stdio_server())
        assert "stdio" in html.lower()
        # It must not be mislabelled as an HTTP transport.
        assert "Streamable HTTP" not in html

    def test_service_identity_is_stated(self) -> None:
        html = _env().get_template("_health_badge.html").render(server=_stdio_server())
        assert "service identity" in html.lower(), (
            "a stdio server's card must say every caller shares one identity"
        )

    def test_http_servers_are_unaffected(self) -> None:
        http_server = RegisteredServer(
            id="h", url="https://e.example.com/mcp", mcp_transport="sse", healthy=True
        )
        html = _env().get_template("_health_badge.html").render(server=http_server)
        assert "Streamable HTTP" in html
        assert "service identity" not in html.lower()


class TestDownloads:
    """`mode` defaults to "direct" and the UI never sends it, so a stdio download
    would otherwise emit `BASE_URL = "stdio:echo"` -- a script that cannot run."""

    def _settings(self) -> Settings:
        return Settings(
            server={"http_host": "127.0.0.1", "http_port": 8080},
            auth={"type": "none"},
            stdio={
                "enabled": True,
                "allowed_commands": {"echo": {"command": sys.executable, "args": [str(ECHO)]}},
            },
        )

    def _register(self, client: TestClient) -> None:
        import json

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

    def test_download_targets_the_hub_not_the_synthetic_url(self) -> None:
        app = create_app(self._settings())
        with TestClient(app) as client:
            self._register(client)
            resp = client.post("/ui/server/echo/tool-download-python/echo")

        assert resp.status_code == 200, resp.text
        assert "stdio:echo" not in resp.text, "a script cannot POST to a stdio: URL"
        assert "127.0.0.1:8080/mcp" in resp.text
        assert 'TARGET_SERVER = "echo"' in resp.text

    def test_direct_mode_is_refused_with_a_reason(self) -> None:
        app = create_app(self._settings())
        with TestClient(app) as client:
            self._register(client)
            resp = client.post("/ui/server/echo/tool-download/echo", data={"mode": "direct"})

        assert resp.status_code == 400
        assert "stdio" in resp.text.lower()
