"""Browsing and importing from the registry in the admin UI (Epic 10, Story 10.4).

The provenance half matters more than the browsing half. A server that arrived from
a public index is a different thing from one an operator typed in: it was described
by someone else, and the description can change under you. An operator looking at a
server card months later needs to be able to tell which it was.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import Settings
from mcp_hub.mcp.registry_client import RegistryRecord


class _FakeRegistryClient:
    def __init__(self, records: list[RegistryRecord]) -> None:
        self._records = records
        self.searched: list[str] = []

    async def get(self, name: str, version: str = "latest") -> RegistryRecord | None:
        return next((r for r in self._records if r.name == name), None)

    async def search(self, query: str = "", *, limit: int = 50) -> list[RegistryRecord]:
        self.searched.append(query)
        return (
            [r for r in self._records if query.lower() in r.name.lower()]
            if query
            else self._records
        )


def _records() -> list[RegistryRecord]:
    return [
        RegistryRecord(
            name="com.example/weather",
            version="2.1.0",
            description="Weather lookups.",
            remotes=[
                {
                    "type": "streamable-http",
                    "url": "https://weather.example.com/mcp",
                    "headers": [{"name": "Authorization", "isRequired": True, "isSecret": True}],
                }
            ],
        ),
        RegistryRecord(
            name="com.example/pkg-only",
            version="1.0.0",
            description="A program.",
            packages=[
                {"registryType": "npm", "identifier": "pkg-mcp", "transport": {"type": "stdio"}}
            ],
        ),
    ]


def _app() -> Any:
    app = create_app(Settings(auth={"type": "none"}))
    app.state.registry_client = _FakeRegistryClient(_records())
    return app


class TestBrowse:
    def test_the_registry_page_renders(self) -> None:
        with TestClient(_app()) as client:
            resp = client.get("/ui/registry")
        assert resp.status_code == 200
        assert "registry" in resp.text.lower()

    def test_search_lists_matches_with_what_an_operator_needs(self) -> None:
        with TestClient(_app()) as client:
            resp = client.post("/ui/registry/search", data={"q": "weather"})

        assert resp.status_code == 200
        body = resp.text
        assert "com.example/weather" in body
        assert "Weather lookups." in body
        assert "2.1.0" in body
        # A credential requirement has to be visible before importing, not after.
        assert "credential" in body.lower() or "authorization" in body.lower()

    def test_a_package_record_is_shown_as_not_directly_importable(self) -> None:
        with TestClient(_app()) as client:
            resp = client.post("/ui/registry/search", data={"q": "pkg-only"})

        body = resp.text
        assert "com.example/pkg-only" in body
        assert "stdio" in body.lower()
        assert "allowed_commands" in body

    def test_search_is_admin_gated_like_registration(self) -> None:
        """Browsing is harmless, but the button next to each result registers. Gating
        the page keeps the two from diverging."""
        from unittest.mock import patch

        from mcp_hub.auth.principal import Principal

        class _Auth:
            async def authenticate(self, request: Any) -> Principal:
                return Principal(
                    subject="u", issuer="https://i", scopes=frozenset({"nope"}), token="t"
                )

        settings = Settings(
            auth={
                "type": "jwt",
                "jwt": {"issuer": "https://i", "audience": "a", "jwks_uri": "https://j"},
            }
        )
        with patch("mcp_hub.app.build_authenticator", return_value=_Auth()):
            app = create_app(settings)
        app.state.registry_client = _FakeRegistryClient(_records())

        with TestClient(app) as client:
            resp = client.post("/ui/registry/search", data={"q": "weather"})
        assert resp.status_code == 403


class TestProvenance:
    @pytest.fixture(autouse=True)
    def _reachable(self) -> Any:
        from unittest.mock import patch

        async def _ok(url: str, require_reachability: bool, allow_private: bool) -> Any:
            return True, "", ["203.0.113.1"]

        with patch("mcp_hub.routes.v1.is_url_safe_for_discovery", _ok):
            yield

    def test_an_imported_server_records_where_it_came_from(self) -> None:
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/v1/registry/import", content=json.dumps({"name": "com.example/weather"})
            )
            assert resp.status_code in (200, 201), resp.text
            stored = app.state.storage._data["com.example.weather"]

        assert stored.registry_name == "com.example/weather"
        assert stored.registry_version == "2.1.0"
        assert stored.registry_source, "which registry it came from must be recorded"
        assert stored.imported_at is not None

    def test_a_hand_registered_server_has_no_provenance(self) -> None:
        app = _app()
        with TestClient(app) as client:
            client.post(
                "/v1/register",
                content=json.dumps({"id": "typed", "url": "https://typed.example.com/mcp"}),
            )
            stored = app.state.storage._data["typed"]

        assert stored.registry_name == ""
        assert stored.imported_at is None

    def test_the_card_shows_the_provenance(self) -> None:
        from pathlib import Path

        from jinja2 import Environment, FileSystemLoader, select_autoescape

        from mcp_hub.models.server import RegisteredServer
        from mcp_hub.utils import dom_id

        env = Environment(
            loader=FileSystemLoader(
                str(Path(__file__).parent.parent / "src" / "mcp_hub" / "templates")
            ),
            autoescape=select_autoescape(["html", "xml"]),
        )
        env.filters["dom_id"] = dom_id

        server = RegisteredServer(
            id="com.example.weather",
            url="https://weather.example.com/mcp",
            registry_name="com.example/weather",
            registry_version="2.1.0",
            registry_source="https://registry.modelcontextprotocol.io",
            healthy=True,
        )
        html = env.get_template("_health_badge.html").render(server=server)
        assert "com.example/weather" in html
        assert "2.1.0" in html
