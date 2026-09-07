"""Importing goes through the ordinary registration path (Epic 10, Story 10.3).

This is the security-relevant story. A registry record is attacker-influenceable
input that names a URL the hub will go on to probe on a timer, so import must not be
a side door around the checks a typed registration goes through — SSRF validation and
the admin requirement above all.
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
    """Stands in for the network. Returns whatever record the test wants.

    The signature mirrors `RegistryClient.get` exactly, including `version`. An
    earlier version of this double took only `name`, so the route's two-argument call
    raised TypeError and surfaced as a 502 -- a test failure that looked like a
    routing bug rather than a double that had drifted from the interface it stands in
    for. Checked with a signature assertion below so it cannot drift again silently.
    """

    def __init__(self, records: dict[str, RegistryRecord]) -> None:
        self._records = records
        self.asked_for: list[tuple[str, str]] = []

    async def get(self, name: str, version: str = "latest") -> RegistryRecord | None:
        self.asked_for.append((name, version))
        return self._records.get(name)


def test_the_double_matches_the_real_client() -> None:
    import inspect

    from mcp_hub.mcp.registry_client import RegistryClient

    real = inspect.signature(RegistryClient.get).parameters
    fake = inspect.signature(_FakeRegistryClient.get).parameters
    assert list(real) == list(fake), "the fake registry client has drifted from the real one"


def _record(url: str, name: str = "com.example/thing") -> RegistryRecord:
    return RegistryRecord(
        name=name,
        version="1.0.0",
        description="a server",
        remotes=[{"type": "streamable-http", "url": url}],
    )


def _app(records: dict[str, RegistryRecord], settings: Settings | None = None) -> Any:
    app = create_app(settings or Settings(auth={"type": "none"}))
    app.state.registry_client = _FakeRegistryClient(records)
    return app


def _import(client: TestClient, name: str, **extra: Any) -> Any:
    body = {"name": name}
    body.update(extra)
    return client.post("/v1/registry/import", content=json.dumps(body))


class TestSsrfValidationApplies:
    def test_a_link_local_url_is_refused(self) -> None:
        """The cloud-metadata endpoint is the canonical case: a record naming it must
        be refused exactly as a typed registration naming it would be."""
        records = {"com.example/evil": _record("http://169.254.169.254/mcp", "com.example/evil")}
        with TestClient(_app(records)) as client:
            resp = _import(client, "com.example/evil")

        assert resp.status_code == 400
        assert "URL validation failed" in resp.text

    def test_an_unreachable_public_url_is_refused_like_any_other(self) -> None:
        records = {
            "com.example/nope": _record("https://no-such-host.invalid/mcp", "com.example/nope")
        }
        with TestClient(_app(records)) as client:
            resp = _import(client, "com.example/nope")

        assert resp.status_code == 400


class TestAdminIsRequired:
    def _jwt_settings(self) -> Settings:
        return Settings(
            auth={
                "type": "jwt",
                "jwt": {"issuer": "https://i", "audience": "a", "jwks_uri": "https://j"},
            }
        )

    def _app_as(self, scopes: set[str], records: dict[str, RegistryRecord]) -> Any:
        from unittest.mock import patch

        from mcp_hub.auth.principal import Principal

        class _Auth:
            async def authenticate(self, request: Any) -> Principal:
                return Principal(
                    subject="u", issuer="https://i", scopes=frozenset(scopes), token="t"
                )

        with patch("mcp_hub.app.build_authenticator", return_value=_Auth()):
            app = create_app(self._jwt_settings())
        app.state.registry_client = _FakeRegistryClient(records)
        return app

    def test_a_non_admin_cannot_import(self) -> None:
        # Importing is registering. It must not be reachable by anyone who could not
        # have registered the same server by hand.
        records = {"com.example/thing": _record("https://e.example.com/mcp")}
        with TestClient(self._app_as({"some:scope"}, records)) as client:
            resp = _import(client, "com.example/thing")

        assert resp.status_code == 403


class TestUnknownAndUnsupportedRecords:
    def test_an_unknown_record_is_404(self) -> None:
        with TestClient(_app({})) as client:
            resp = _import(client, "com.example/missing")
        assert resp.status_code == 404

    def test_a_package_only_record_is_refused_with_the_stdio_instructions(self) -> None:
        record = RegistryRecord(
            name="com.example/pkg",
            version="1.0.1",
            packages=[
                {"registryType": "npm", "identifier": "pkg-mcp", "transport": {"type": "stdio"}}
            ],
        )
        with TestClient(_app({"com.example/pkg": record})) as client:
            resp = _import(client, "com.example/pkg")

        assert resp.status_code == 400
        assert "allowed_commands" in resp.text
        assert "pkg-mcp" in resp.text


class TestReimport:
    """Re-importing must behave like re-registering: update, never duplicate, and
    never blank a credential the operator supplied afterwards.

    URL *reachability* is stubbed out here. Import deliberately keeps the same
    reachability requirement as a typed manual registration -- that is the whole point
    of sharing the path -- but these tests are about merge semantics, and a test that
    needs a live endpoint to prove a merge rule would be testing the network. The
    safety half of that check has its own tests in TestSsrfValidationApplies, which do
    not stub anything.
    """

    @pytest.fixture(autouse=True)
    def _reachable(self) -> Any:
        from unittest.mock import patch

        async def _ok(url: str, require_reachability: bool, allow_private: bool) -> Any:
            return True, "", ["203.0.113.1"]

        with patch("mcp_hub.routes.v1.is_url_safe_for_discovery", _ok):
            yield

    def test_reimport_updates_rather_than_duplicating(self) -> None:
        records = {"com.example/thing": _record("https://e.example.com/mcp")}
        app = _app(records)
        with TestClient(app) as client:
            first = _import(client, "com.example/thing")
            assert first.status_code in (200, 201), first.text
            second = _import(client, "com.example/thing")
            assert second.status_code in (200, 201), second.text
            listed = client.get("/v1/servers").json()

        servers = listed if isinstance(listed, list) else listed.get("servers", [])
        assert [s["id"] for s in servers] == ["com.example.thing"]

    def test_reimport_does_not_blank_a_stored_credential(self) -> None:
        records = {"com.example/thing": _record("https://e.example.com/mcp")}
        app = _app(records)
        with TestClient(app) as client:
            _import(client, "com.example/thing")
            # The operator supplies the credential the record could only hint at.
            client.post(
                "/v1/register",
                content=json.dumps(
                    {
                        "id": "com.example.thing",
                        "url": "https://e.example.com/mcp",
                        "auth_type": "bearer",
                        "bearer_token": "secret-token",
                    }
                ),
            )
            _import(client, "com.example/thing")
            stored = app.state.storage._data["com.example.thing"]

        assert stored.bearer_token == "secret-token", "an import must not erase a credential"
        assert stored.auth_type == "bearer"


# Read storage's dict directly rather than awaiting `registry.get`. A helper that did
# `get_event_loop().run_until_complete(...)` passed alone and failed inside the full
# suite: it depends on whatever loop state earlier tests left behind, and
# InMemoryStorage's asyncio.Lock is bound to the loop that created it, so driving it
# from a fresh loop is not safe either. The stored credential is not visible through
# the API by design -- `sanitize_for_api` strips it -- which is exactly why this
# assertion has to reach past it.
