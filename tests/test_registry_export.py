"""Exporting a server as `server.json` (Epic 10, Story 10.5).

Export writes a file for the operator to review and publish themselves. The hub does
not publish (ADR 0008), so this is the moment a human sees what would become public —
which is exactly why it has to be honest about secrets and about URLs that should
never leave the building.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcp_hub.mcp.registry_export import (
    SERVER_JSON_SCHEMA_URL,
    export_warnings,
    to_server_json,
)
from mcp_hub.models.server import RegisteredServer


def _server(**over: Any) -> RegisteredServer:
    base: dict[str, Any] = {
        "id": "com.example.thing",
        "url": "https://thing.example.com/mcp",
        "name": "Thing",
        "description": "Does things.",
        "version": "1.2.3",
        "mcp_transport": "sse",
    }
    base.update(over)
    return RegisteredServer(**base)


class TestShape:
    def test_produces_the_registry_schema(self) -> None:
        doc = to_server_json(_server())
        assert doc["$schema"] == SERVER_JSON_SCHEMA_URL
        assert doc["name"]
        assert doc["version"] == "1.2.3"
        assert doc["remotes"][0]["url"] == "https://thing.example.com/mcp"

    def test_transport_is_the_registry_spelling_not_the_hubs(self) -> None:
        """The hub stores "sse" as its marker for streamable HTTP. The registry means
        legacy SSE by that word, so exporting the stored value verbatim would publish a
        false claim about the server's transport."""
        doc = to_server_json(_server(mcp_transport="sse"))
        assert doc["remotes"][0]["type"] == "streamable-http"

    def test_an_imported_server_keeps_its_registry_name(self) -> None:
        # Round-tripping should not rename someone else's server.
        doc = to_server_json(_server(registry_name="ai.vendor/thing"))
        assert doc["name"] == "ai.vendor/thing"

    def test_a_hand_registered_id_becomes_a_namespaced_name(self) -> None:
        # The registry requires reverse-DNS namespacing; a bare hub id is not one.
        doc = to_server_json(_server(id="my-server", registry_name=""))
        assert "/" in doc["name"], "a registry name needs a namespace"

    def test_a_stdio_server_exports_as_a_package_not_a_remote(self) -> None:
        doc = to_server_json(
            _server(url="stdio:echo", transport_kind="stdio", stdio_command_name="echo")
        )
        assert not doc.get("remotes")
        assert doc.get("packages"), "a program is a package, not a URL"


class TestSecrets:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("bearer_token", "super-secret"),
            ("basic_password", "hunter2"),
            ("oauth_client_secret", "client-secret"),
        ],
    )
    def test_no_credential_reaches_the_file(self, field: str, value: str) -> None:
        doc = to_server_json(_server(auth_type="bearer", **{field: value}))
        assert value not in json.dumps(doc), f"{field} leaked into an exported file"

    def test_the_basic_username_does_not_travel_either(self) -> None:
        # Not a secret, but still an operational detail of this deployment rather than
        # a property of the server everyone else would use.
        doc = to_server_json(_server(auth_type="basic", basic_username="admin"))
        assert "admin" not in json.dumps(doc)

    def test_a_required_credential_is_declared_without_its_value(self) -> None:
        doc = to_server_json(_server(auth_type="bearer", bearer_token="super-secret"))
        headers = doc["remotes"][0].get("headers") or []
        assert any(h["name"].lower() == "authorization" for h in headers), (
            "a server needing a credential should say so, just not what it is"
        )
        assert "super-secret" not in json.dumps(headers)


class TestWarnings:
    def test_a_loopback_url_is_named(self) -> None:
        warnings = export_warnings(_server(url="http://127.0.0.1:9000/mcp"))
        assert any("127.0.0.1" in w for w in warnings)

    def test_a_private_range_url_is_named(self) -> None:
        warnings = export_warnings(_server(url="http://192.168.1.20/mcp"))
        assert any("192.168.1.20" in w for w in warnings)

    def test_an_internal_hostname_is_named(self) -> None:
        # Not resolvable to a range here, but a bare hostname with no dot is a LAN name
        # far more often than a public one.
        warnings = export_warnings(_server(url="http://octopus/webcalendar/mcp.php"))
        assert warnings

    def test_a_public_url_produces_no_address_warning(self) -> None:
        # Scoped to address warnings: a hand-registered server also warns that its
        # name was generated, which is true and unrelated to the URL.
        warnings = export_warnings(
            _server(url="https://thing.example.com/mcp", registry_name="ai.vendor/thing")
        )
        assert not [w for w in warnings if "thing.example.com" in w]

    def test_required_scope_is_flagged_as_local_policy(self) -> None:
        warnings = export_warnings(_server(required_scope="files:use"))
        assert any("required_scope" in w or "scope" in w.lower() for w in warnings)


class TestExportRoute:
    def _app(self) -> Any:
        from mcp_hub.app import create_app
        from mcp_hub.config import Settings

        return create_app(Settings(auth={"type": "none"}))

    def _register(self, client: Any, **over: Any) -> None:
        body = {"id": "com.example.thing", "url": "https://thing.example.com/mcp"}
        body.update(over)
        from unittest.mock import patch

        async def _ok(url: str, require_reachability: bool, allow_private: bool) -> Any:
            return True, "", ["203.0.113.1"]

        with patch("mcp_hub.routes.v1.is_url_safe_for_discovery", _ok):
            resp = client.post("/v1/register", content=json.dumps(body))
        assert resp.status_code in (200, 201), resp.text

    def test_it_downloads_a_document_and_its_warnings(self) -> None:
        from fastapi.testclient import TestClient

        with TestClient(self._app()) as client:
            self._register(client)
            resp = client.get("/v1/servers/com.example.thing/server.json")

        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["server"]["$schema"] == SERVER_JSON_SCHEMA_URL
        assert "attachment" in resp.headers.get("content-disposition", "")
        # A hand-registered server always has at least the generated-name warning.
        assert payload["warnings"]

    def test_a_credential_never_reaches_the_export(self) -> None:
        from fastapi.testclient import TestClient

        with TestClient(self._app()) as client:
            self._register(client, auth_type="bearer", bearer_token="super-secret")
            resp = client.get("/v1/servers/com.example.thing/server.json")

        assert "super-secret" not in resp.text

    def test_a_private_url_is_warned_about(self) -> None:
        from fastapi.testclient import TestClient

        with TestClient(self._app()) as client:
            self._register(client, id="internal", url="http://192.168.1.20/mcp")
            resp = client.get("/v1/servers/internal/server.json")

        assert any("192.168.1.20" in w for w in resp.json()["warnings"])
        assert resp.headers.get("X-Export-Warnings")

    def test_unknown_server_is_404(self) -> None:
        from fastapi.testclient import TestClient

        with TestClient(self._app()) as client:
            assert client.get("/v1/servers/nope/server.json").status_code == 404
