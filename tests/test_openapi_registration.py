"""Story 13.4 (part C) -- registering and managing OpenAPI servers over /v1/openapi.

Driven through the real app: imports are background jobs whose spec load runs in the
isolated child, so these use `with TestClient(app)`, which keeps the event loop
alive between requests, and poll each job to completion.
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import AuthConfig, HealthCheckConfig, JWTAuthConfig, SecurityConfig, Settings
from mcp_hub.models.server import RegisteredServer

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "openapi"
PETSTORE = (FIXTURES / "petstore-3.1.json").read_text()
BASE_URL = "http://127.0.0.1:9/v1"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "healthcheck": HealthCheckConfig(enabled=False),
        "security": SecurityConfig(allow_private_networks=True),
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def client():
    with TestClient(create_app(_settings()), raise_server_exceptions=False) as test_client:
        yield test_client


def _import(client: TestClient, **body: Any) -> dict[str, Any]:
    payload = {"id": "pets", "spec": PETSTORE, "base_url": BASE_URL, **body}
    response = client.post("/v1/openapi/imports", json=payload)
    assert response.status_code == 202, response.text
    return _wait(client, response.json()["job_id"])


def _wait(client: TestClient, job_id: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = client.get(f"/v1/openapi/imports/{job_id}", headers=headers or {}).json()
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish")


def _server(client: TestClient, server_id: str = "pets") -> RegisteredServer | None:
    return client.portal.call(client.app.state.registry.get, server_id)  # type: ignore[union-attr]


def _changed_petstore() -> str:
    doc = json.loads(PETSTORE)
    doc["paths"]["/pets"]["get"]["summary"] = "List every pet"
    return json.dumps(doc)


# --------------------------------------------------------------------------- import


class TestImport:
    def test_import_creates_a_server_with_nothing_exposed(self, client):
        response = client.post(
            "/v1/openapi/imports", json={"id": "pets", "spec": PETSTORE, "base_url": BASE_URL}
        )
        assert response.status_code == 202
        body = response.json()
        assert response.headers["location"] == f"/v1/openapi/imports/{body['job_id']}"
        job = _wait(client, body["job_id"])
        assert (job["status"], job["outcome"], job["server_id"]) == ("succeeded", "created", "pets")

        server = _server(client)
        assert server.is_openapi and server.url == "openapi:pets"
        assert server.mcp_transport == "openapi"
        assert server.openapi_base_url == BASE_URL
        assert server.openapi_snapshot.dialect == "3.1"
        assert server.openapi_selection == []
        assert server.tools == []

    def test_import_goes_through_the_isolated_child(self, client, monkeypatch):
        import mcp_hub.openapi.isolation as isolation

        calls: list[int] = []
        real = isolation._load_in_child

        def spy(data, limits):
            calls.append(len(data))
            return real(data, limits)

        monkeypatch.setattr(isolation, "_load_in_child", spy)
        assert _import(client)["status"] == "succeeded"
        assert calls == [len(PETSTORE.encode())]

    def test_importer_never_imports_the_unsandboxed_loader(self):
        source = (
            pathlib.Path(__file__).parent.parent / "src/mcp_hub/openapi/importer.py"
        ).read_text()
        assert "load_spec_unsandboxed" not in source

    def test_selection_overrides_and_probe_at_import(self, client):
        job = _import(
            client,
            selection=[{"method": "get", "path": "/pets"}],
            overrides=[{"method": "GET", "path": "/pets", "name": "list_pets"}],
            health_probe={"method": "GET", "path": "/pets"},
        )
        assert job["status"] == "succeeded"
        server = _server(client)
        assert [t["name"] for t in server.tools] == ["list_pets"]
        assert server.openapi_health_probe.path == "/pets"

    def test_an_invalid_spec_fails_the_job_and_registers_nothing(self, client):
        doc = json.loads(PETSTORE)
        doc["info"]["title"] = 7
        job = _import(client, spec=json.dumps(doc))
        assert job["status"] == "failed"
        assert job["pointer"] == "/info/title"
        assert _server(client) is None

    def test_an_unservable_selection_fails_the_job(self, client):
        job = _import(
            client,
            selection=[
                {"method": "GET", "path": "/pets"},
                {"method": "GET", "path": "/pets/{petId}"},
            ],
            overrides=[
                {"method": "GET", "path": "/pets", "name": "same"},
                {"method": "GET", "path": "/pets/{petId}", "name": "same"},
            ],
        )
        assert job["status"] == "failed"
        assert len(job["problems"]) == 2
        assert _server(client) is None

    def test_credentials_are_stored_and_redacted(self, client):
        _import(
            client,
            auth_type="api_key",
            api_key_name="X-Key",
            api_key_in="header",
            api_key_value="s3cret",
        )
        assert _server(client).api_key_value == "s3cret"
        listed = client.get("/v1/servers").json()
        assert "s3cret" not in json.dumps(listed)


class TestRequestValidation:
    @pytest.mark.parametrize(
        "body, fragment",
        [
            ({"spec": PETSTORE, "spec_url": "http://127.0.0.1:9/spec.json"}, "exactly one"),
            ({}, "exactly one"),
            ({"spec": PETSTORE, "base_url": "ftp://127.0.0.1/x"}, "base_url"),
            ({"spec": PETSTORE, "auth_type": "api_key", "api_key_in": "header"}, "api_key_name"),
            (
                {
                    "spec": PETSTORE,
                    "auth_type": "api_key",
                    "api_key_name": "k",
                    "api_key_in": "cookie",
                },
                "api_key_in",
            ),
            (
                {
                    "spec": PETSTORE,
                    "auth_type": "api_key",
                    "api_key_name": "Host",
                    "api_key_in": "header",
                },
                "api_key_name",
            ),
            ({"spec": PETSTORE, "auth_type": "oauth"}, "oauth_token_url"),
        ],
    )
    def test_rejected_before_any_job(self, client, body, fragment):
        payload = {"id": "pets", "base_url": BASE_URL, **body}
        response = client.post("/v1/openapi/imports", json=payload)
        assert response.status_code == 400, response.text
        assert fragment in response.text

    def test_private_base_url_is_refused_like_any_backend(self):
        with TestClient(
            create_app(_settings(security=SecurityConfig(allow_private_networks=False)))
        ) as client:
            response = client.post(
                "/v1/openapi/imports", json={"id": "pets", "spec": PETSTORE, "base_url": BASE_URL}
            )
        assert response.status_code == 400
        assert response.json() == {"error": "URL validation failed"}

    def test_private_spec_url_is_refused(self):
        with TestClient(
            create_app(_settings(security=SecurityConfig(allow_private_networks=False)))
        ) as client:
            response = client.post(
                "/v1/openapi/imports",
                json={
                    "id": "pets",
                    "spec_url": "http://169.254.169.254/latest",
                    "base_url": "https://api.example.com",
                },
            )
        assert response.status_code == 400

    def test_oversized_spec_is_refused(self):
        settings = _settings()
        settings.openapi.max_spec_bytes = 100
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/v1/openapi/imports", json={"id": "pets", "spec": PETSTORE, "base_url": BASE_URL}
            )
        assert response.status_code == 413

    def test_id_of_a_non_openapi_server_is_a_conflict(self, client):
        client.portal.call(
            client.app.state.registry.register,
            RegisteredServer(id="pets", url="https://mcp.example.com/mcp"),
        )
        response = client.post(
            "/v1/openapi/imports", json={"id": "pets", "spec": PETSTORE, "base_url": BASE_URL}
        )
        assert response.status_code == 409

    def test_unknown_job(self, client):
        assert client.get("/v1/openapi/imports/nope").status_code == 404


class TestGenericRegistrationRefuses:
    def test_register_cannot_create_an_openapi_server(self, client):
        response = client.post(
            "/v1/register",
            json={"id": "x", "transport_kind": "openapi", "url": "https://a.example"},
        )
        assert response.status_code == 400

    def test_register_cannot_overwrite_an_openapi_server(self, client):
        _import(client)
        response = client.post("/v1/register", json={"id": "pets", "url": "http://127.0.0.1:9/mcp"})
        assert response.status_code == 409
        assert _server(client).is_openapi


# --------------------------------------------------------------------------- re-import


class TestReimport:
    def test_omitted_choices_are_kept(self, client):
        _import(
            client,
            selection=[{"method": "GET", "path": "/pets"}],
            overrides=[{"method": "GET", "path": "/pets", "name": "lp"}],
        )
        job = _import(client)
        assert job["outcome"] == "spec_unchanged"
        server = _server(client)
        assert [r.path for r in server.openapi_selection] == ["/pets"]
        assert server.openapi_overrides[0].name == "lp"

    def test_explicit_empty_clears(self, client):
        _import(
            client,
            selection=[{"method": "GET", "path": "/pets"}],
            health_probe={"method": "GET", "path": "/pets"},
        )
        _import(client, selection=[], health_probe=None)
        server = _server(client)
        assert server.openapi_selection == []
        assert server.openapi_health_probe is None
        assert server.tools == []

    def test_omitted_credentials_are_kept(self, client):
        _import(client, auth_type="bearer", bearer_token="t1")
        _import(client)
        assert _server(client).bearer_token == "t1"

    def test_a_changed_spec_waits_as_pending(self, client):
        _import(client, selection=[{"method": "GET", "path": "/pets"}])
        accepted = _server(client).openapi_snapshot.sha256
        job = _import(client, spec=_changed_petstore())
        assert job["outcome"] == "revision_pending"
        server = _server(client)
        assert server.openapi_snapshot.sha256 == accepted
        assert server.openapi_pending is not None and server.openapi_pending.sha256 != accepted
        # Live tools still come from the accepted snapshot.
        assert server.tools[0]["description"] == "List all pets"

    def test_base_url_can_change_on_reimport(self, client):
        _import(client)
        _import(client, base_url="http://127.0.0.1:9/v2")
        assert _server(client).openapi_base_url == "http://127.0.0.1:9/v2"


# --------------------------------------------------------------------------- operations and tools


class TestOperationsAndTools:
    def test_preview_lists_every_operation(self, client):
        _import(client, selection=[{"method": "GET", "path": "/pets"}])
        body = client.get("/v1/openapi/servers/pets/operations").json()
        assert body["pending"] is None
        by_key = {(op["method"], op["path"]): op for op in body["operations"]}
        assert set(by_key) == {("GET", "/pets"), ("POST", "/pets"), ("GET", "/pets/{petId}")}
        assert by_key[("GET", "/pets")]["selected"] is True
        assert by_key[("POST", "/pets")]["tool"]["name"] == "createPet"
        assert by_key[("POST", "/pets")]["unsupported"] == []

    def test_preview_shows_a_pending_revision(self, client):
        _import(client)
        _import(client, spec=_changed_petstore())
        assert client.get("/v1/openapi/servers/pets/operations").json()["pending"]["sha256"]

    def test_put_selection(self, client):
        _import(client)
        response = client.put(
            "/v1/openapi/servers/pets/tools",
            json={"selection": [{"method": "POST", "path": "/pets"}]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["tools"] == ["createPet"]
        assert [t["name"] for t in _server(client).tools] == ["createPet"]

    def test_put_omitted_fields_are_kept(self, client):
        _import(client, selection=[{"method": "GET", "path": "/pets"}])
        client.put(
            "/v1/openapi/servers/pets/tools",
            json={"overrides": [{"method": "GET", "path": "/pets", "name": "renamed"}]},
        )
        server = _server(client)
        assert [r.path for r in server.openapi_selection] == ["/pets"]
        assert [t["name"] for t in server.tools] == ["renamed"]

    def test_put_unservable_is_refused_and_changes_nothing(self, client):
        _import(client, selection=[{"method": "GET", "path": "/pets"}])
        response = client.put(
            "/v1/openapi/servers/pets/tools",
            json={"overrides": [{"method": "GET", "path": "/pets", "name": "not valid"}]},
        )
        assert response.status_code == 400
        assert response.json()["problems"]
        assert _server(client).openapi_overrides == []

    @pytest.mark.parametrize(
        "probe, fragment",
        [
            ({"method": "POST", "path": "/pets"}, "GET"),
            ({"method": "GET", "path": "/pets/{petId}"}, "required"),
            ({"method": "GET", "path": "/nope"}, "not in the spec"),
        ],
    )
    def test_invalid_health_probes(self, client, probe, fragment):
        _import(client)
        response = client.put("/v1/openapi/servers/pets/tools", json={"health_probe": probe})
        assert response.status_code == 400
        assert fragment in json.dumps(response.json())

    def test_probe_need_not_be_selected(self, client):
        _import(client)
        response = client.put(
            "/v1/openapi/servers/pets/tools",
            json={"health_probe": {"method": "GET", "path": "/pets"}},
        )
        assert response.status_code == 200
        assert _server(client).openapi_health_probe.path == "/pets"

    def test_null_probe_clears(self, client):
        _import(client, health_probe={"method": "GET", "path": "/pets"})
        client.put("/v1/openapi/servers/pets/tools", json={"health_probe": None})
        assert _server(client).openapi_health_probe is None

    @pytest.mark.parametrize("route", ["/v1/openapi/servers/nope/operations"])
    def test_unknown_server(self, client, route):
        assert client.get(route).status_code == 404
        assert client.put("/v1/openapi/servers/nope/tools", json={}).status_code == 404

    def test_non_openapi_server(self, client):
        client.portal.call(
            client.app.state.registry.register,
            RegisteredServer(id="m", url="https://mcp.example.com/mcp"),
        )
        assert client.get("/v1/openapi/servers/m/operations").status_code == 404


# --------------------------------------------------------------------------- admin under jwt


class TestAdminScope:
    @pytest.fixture
    def jwt_client(self):
        from tests.test_multitenant_integration import AUDIENCE, ISSUER, JWKS_URI, jwks_client

        settings = _settings(
            auth=AuthConfig(
                type="jwt", jwt=JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, jwks_uri=JWKS_URI)
            )
        )
        app = create_app(settings)
        app.state.authenticator._client = jwks_client()  # type: ignore[attr-defined]
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client

    @staticmethod
    def _headers(*scopes: str) -> dict[str, str]:
        from tests.test_multitenant_integration import token_for

        return {"Authorization": f"Bearer {token_for('alice', *scopes)}"}

    def test_non_admin_is_refused_everywhere(self, jwt_client):
        user = self._headers("files:use")
        body = {"id": "pets", "spec": PETSTORE, "base_url": BASE_URL}
        assert jwt_client.post("/v1/openapi/imports", json=body, headers=user).status_code == 403
        assert jwt_client.get("/v1/openapi/imports/x", headers=user).status_code == 403
        assert (
            jwt_client.get("/v1/openapi/servers/pets/operations", headers=user).status_code == 403
        )
        assert (
            jwt_client.put("/v1/openapi/servers/pets/tools", json={}, headers=user).status_code
            == 403
        )

    def test_anonymous_is_refused(self, jwt_client):
        assert jwt_client.post("/v1/openapi/imports", json={}).status_code == 401

    def test_admin_may(self, jwt_client):
        from mcp_hub.auth.authorize import DEFAULT_ADMIN_SCOPE

        admin = self._headers(DEFAULT_ADMIN_SCOPE)
        body = {"id": "pets", "spec": PETSTORE, "base_url": BASE_URL}
        response = jwt_client.post("/v1/openapi/imports", json=body, headers=admin)
        assert response.status_code == 202
        assert _wait(jwt_client, response.json()["job_id"], headers=admin)["status"] == "succeeded"
        put = jwt_client.put(
            "/v1/openapi/servers/pets/tools",
            json={"selection": [{"method": "GET", "path": "/pets"}]},
            headers=admin,
        )
        assert put.status_code == 200


# --------------------------------------------------------------------------- remaining paths


@pytest.fixture
def spec_server():
    """A real HTTP server on loopback serving the petstore spec, so a spec_url import
    goes through the actual SSRF-pinned fetch."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 -- http.server's naming
            body = PETSTORE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/openapi.json"
    httpd.shutdown()


class TestRemainingPaths:
    def test_import_from_a_spec_url(self, client, spec_server):
        response = client.post(
            "/v1/openapi/imports",
            json={"id": "pets", "spec_url": spec_server, "base_url": BASE_URL},
        )
        job = _wait(client, response.json()["job_id"])
        assert job["status"] == "succeeded", job
        assert _server(client).openapi_snapshot.source_url == spec_server

    def test_invalid_json(self, client):
        response = client.post(
            "/v1/openapi/imports", content=b"{nope", headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400

    def test_queue_full_is_429(self, client, monkeypatch):
        from mcp_hub.openapi.jobs import JobQueueFull

        def full(*args: Any, **kwargs: Any) -> Any:
            raise JobQueueFull("busy")

        monkeypatch.setattr(client.app.state.openapi_jobs, "submit", full)
        response = client.post(
            "/v1/openapi/imports", json={"id": "pets", "spec": PETSTORE, "base_url": BASE_URL}
        )
        assert response.status_code == 429

    def test_put_invalid_body(self, client):
        _import(client)
        response = client.put("/v1/openapi/servers/pets/tools", json={"selection": "not a list"})
        assert response.status_code == 400

    def test_reimport_replaces_tags_and_overrides_when_sent(self, client):
        _import(client, tags=["a"], overrides=[{"method": "GET", "path": "/pets", "name": "x"}])
        _import(client, tags=["b"], overrides=[])
        server = _server(client)
        assert server.tags == ["b"]
        assert server.openapi_overrides == []


class TestJobTimeRechecks:
    """What the route checked can change before the job finishes; the job re-checks."""

    @staticmethod
    async def _run(registry: Any, **fields: Any) -> str:
        from mcp_hub.openapi.importer import OpenAPIImportRequest, ServerLocks, run_import

        class NoDiscovery:
            async def discover_immediately(self, server: Any) -> None:
                pass

        request = OpenAPIImportRequest(id="pets", spec=PETSTORE, **fields)
        return await run_import(
            request,
            registry=registry,
            discovery_service=NoDiscovery(),
            settings=_settings(),
            locks=ServerLocks(),
        )

    async def test_id_taken_by_a_non_openapi_server_meanwhile(self):
        from mcp_hub.openapi.importer import ImportRejected
        from mcp_hub.registry.service import Registry
        from mcp_hub.storage.memory import InMemoryStorage

        registry = Registry(InMemoryStorage())
        await registry.register(RegisteredServer(id="pets", url="https://mcp.example.com/mcp"))
        with pytest.raises(ImportRejected, match="non-OpenAPI"):
            await self._run(registry, base_url=BASE_URL)

    async def test_new_server_without_a_base_url_is_a_draft(self):
        # Story 13.8: spec first, base URL confirmed afterwards from servers[].
        from mcp_hub.registry.service import Registry
        from mcp_hub.storage.memory import InMemoryStorage

        registry = Registry(InMemoryStorage())
        assert await self._run(registry) == "created"
        assert (await registry.get("pets")).openapi_base_url == ""


class TestReviewFixes:
    """Security review of Story 13.4 (findings 1, 2, 3, 4, 7)."""

    def test_listings_carry_no_spec_document(self, client):
        _import(client)
        for route in ("/v1/servers", "/v1/servers/pets"):
            assert '"paths"' not in client.get(route).text

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://user:pw@127.0.0.1:9/v1",
            "http://127.0.0.1:9/v1?key=secret",
            "http://127.0.0.1:9/v1#frag",
        ],
    )
    def test_base_url_must_not_carry_secrets_or_a_query(self, client, base_url):
        response = client.post(
            "/v1/openapi/imports", json={"id": "pets", "spec": PETSTORE, "base_url": base_url}
        )
        assert response.status_code == 400
        assert "base_url" in response.text

    @pytest.mark.parametrize("value", ["line\nbreak", "tab\there", "nul\x00"])
    def test_api_key_value_must_be_a_header_safe_string(self, client, value):
        body = {
            "id": "pets",
            "spec": PETSTORE,
            "base_url": BASE_URL,
            "auth_type": "api_key",
            "api_key_name": "k",
            "api_key_in": "header",
            "api_key_value": value,
        }
        response = client.post("/v1/openapi/imports", json=body)
        assert response.status_code == 400
        assert "api_key_value" in response.text

    @pytest.mark.parametrize("path", ["/pets/../admin", "/pets?x=1", "/pets#f"])
    def test_probe_path_must_be_a_plain_path(self, client, path):
        doc = json.loads(PETSTORE)
        doc["paths"][path] = {"get": {"responses": {"200": {"description": "ok"}}}}
        _import(client, spec=json.dumps(doc))
        response = client.put(
            "/v1/openapi/servers/pets/tools", json={"health_probe": {"method": "GET", "path": path}}
        )
        assert response.status_code == 400
        assert "plain path" in response.text

    def test_declared_oversize_body_is_refused_unread(self, client):
        response = client.post(
            "/v1/openapi/imports",
            content=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": str(10**12)},
        )
        assert response.status_code == 413

    def test_put_body_is_capped(self, client):
        _import(client)
        response = client.put(
            "/v1/openapi/servers/pets/tools",
            content=b" " * (2 * 1024 * 1024),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413

    def test_concurrent_puts_do_not_lose_updates(self, client, monkeypatch):
        # Each PUT is a read-modify-write of the whole record; without a per-server
        # lock, two at once can each write back the other's stale copy. The delay
        # inside the (threaded) mapping step makes the two overlap every time.
        import concurrent.futures

        import mcp_hub.routes.openapi as routes

        real = routes.apply_choices

        def slow(*args: Any, **kwargs: Any) -> Any:
            time.sleep(0.3)
            return real(*args, **kwargs)

        monkeypatch.setattr(routes, "apply_choices", slow)
        _import(client)

        def put(body: dict[str, Any]) -> int:
            return client.put("/v1/openapi/servers/pets/tools", json=body).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(
                pool.map(
                    put,
                    [
                        {"selection": [{"method": "GET", "path": "/pets"}]},
                        {"health_probe": {"method": "GET", "path": "/pets"}},
                    ],
                )
            )
        assert codes == [200, 200]
        server = _server(client)
        assert [r.path for r in server.openapi_selection] == ["/pets"]
        assert server.openapi_health_probe is not None
