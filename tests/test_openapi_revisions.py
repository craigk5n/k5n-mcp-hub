"""Story 13.7 -- a changed spec waits for an admin (ADR 0011 §2).

A refresh (re-fetch from the stored URL, or a new upload) that changes the SHA-256
becomes the *pending* revision; the live tools keep serving the accepted snapshot.
The admin reads a diff, then accepts it -- choices carry over by `(METHOD, path)`,
new operations start disabled, orphans are dropped and reported -- or rejects it.
Nothing re-fetches on a timer.
"""

from __future__ import annotations

import copy
import http.server
import json
import pathlib
import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import HealthCheckConfig, SecurityConfig, Settings
from mcp_hub.models import RegisteredServer
from mcp_hub.models.openapi import OpenAPIOperationRef, OpenAPIOverride, OpenAPISnapshot
from mcp_hub.openapi.model import MappingError
from mcp_hub.openapi.revision import accept_revision, diff_revision

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "openapi"
BASE_URL = "http://127.0.0.1:9/v1"
LIMIT = 1024


def _petstore() -> dict[str, Any]:
    return json.loads((FIXTURES / "petstore-3.1.json").read_text())


def _snap(document: dict[str, Any], sha: str, source_url: str = "") -> OpenAPISnapshot:
    return OpenAPISnapshot(document=document, dialect="3.1", sha256=sha, source_url=source_url)


def _server(
    accepted: dict[str, Any], pending: dict[str, Any] | None, **fields: Any
) -> RegisteredServer:
    values: dict[str, Any] = {
        "id": "pets",
        "url": "",
        "transport_kind": "openapi",
        "openapi_base_url": BASE_URL,
        "openapi_snapshot": _snap(accepted, "a" * 64),
        "openapi_pending": _snap(pending, "b" * 64) if pending is not None else None,
        "openapi_selection": [
            OpenAPIOperationRef(method="GET", path="/pets"),
            OpenAPIOperationRef(method="POST", path="/pets"),
        ],
    }
    values.update(fields)
    return RegisteredServer(**values)


def _keys(entries: list[Any]) -> set[tuple[str, str]]:
    return {(e.key.method, e.key.path) for e in entries}


# --------------------------------------------------------------------------- the diff


class TestDiff:
    def test_identical_documents_have_an_empty_diff(self):
        diff = diff_revision(_server(_petstore(), _petstore()), description_limit=LIMIT)
        assert diff.is_empty

    def test_added_and_removed_operations(self):
        pending = _petstore()
        del pending["paths"]["/pets/{petId}"]
        pending["paths"]["/owners"] = {
            "get": {"operationId": "listOwners", "responses": {"200": {"description": "ok"}}}
        }
        diff = diff_revision(_server(_petstore(), pending), description_limit=LIMIT)
        assert _keys(diff.added) == {("GET", "/owners")}
        assert _keys(diff.removed) == {("GET", "/pets/{petId}")}

    def test_input_output_and_description_changes(self):
        pending = _petstore()
        get = pending["paths"]["/pets"]["get"]
        get["parameters"][0]["schema"]["maximum"] = 50
        get["summary"] = "List some pets"
        pending["components"]["schemas"]["Pet"]["allOf"][1]["properties"]["id"]["format"] = "int32"
        diff = diff_revision(_server(_petstore(), pending), description_limit=LIMIT)
        changes = {(c.key.method, c.key.path): set(c.changes) for c in diff.changed}
        assert changes[("GET", "/pets")] == {"input schema", "output schema", "description"}
        assert "output schema" in changes[("POST", "/pets")]  # Pet is its output too

    def test_a_spec_description_change_shows_even_under_an_override(self):
        pending = _petstore()
        pending["paths"]["/pets"]["get"]["summary"] = "List some pets"
        override = OpenAPIOverride(method="GET", path="/pets", description="Admin's own words")
        diff = diff_revision(
            _server(_petstore(), pending, openapi_overrides=[override]), description_limit=LIMIT
        )
        assert {(c.key.method, c.key.path) for c in diff.changed} == {("GET", "/pets")}

    def test_support_changes(self):
        pending = _petstore()
        pending["paths"]["/pets"]["post"]["requestBody"]["content"] = {
            "multipart/form-data": {"schema": {"type": "object"}}
        }
        diff = diff_revision(_server(_petstore(), pending), description_limit=LIMIT)
        (change,) = [c for c in diff.changed if c.key.method == "POST"]
        assert any(c.startswith("now unsupported") for c in change.changes)

    def test_servers_change(self):
        pending = _petstore()
        pending["servers"] = [{"url": "https://new.example.com/v2"}]
        diff = diff_revision(_server(_petstore(), pending), description_limit=LIMIT)
        assert diff.servers == (["https://petstore.example.com/v1"], ["https://new.example.com/v2"])

    def test_swagger_servers_are_host_base_path_and_schemes(self):
        accepted = json.loads((FIXTURES / "swagger2-kitchen-sink.json").read_text())
        pending = copy.deepcopy(accepted)
        pending["host"] = "api2.example.com"
        server = _server(accepted, pending, openapi_selection=[])
        server.openapi_snapshot = OpenAPISnapshot(document=accepted, dialect="2.0", sha256="a" * 64)
        server.openapi_pending = OpenAPISnapshot(document=pending, dialect="2.0", sha256="b" * 64)
        diff = diff_revision(server, description_limit=LIMIT)
        assert diff.servers is not None
        assert diff.servers[1] == ["https://api2.example.com/v2", "http://api2.example.com/v2"]

    def test_orphans(self):
        pending = _petstore()
        del pending["paths"]["/pets"]["post"]
        pending["paths"]["/pets/{petId}"]["get"]["parameters"][0]["style"] = (
            "matrix"  # now unsupported
        )
        server = _server(
            _petstore(),
            pending,
            openapi_selection=[
                OpenAPIOperationRef(method="GET", path="/pets"),
                OpenAPIOperationRef(method="POST", path="/pets"),
                OpenAPIOperationRef(method="GET", path="/pets/{petId}"),
            ],
            openapi_overrides=[OpenAPIOverride(method="POST", path="/pets", name="make_pet")],
            openapi_health_probe=OpenAPIOperationRef(method="GET", path="/pets/{petId}"),
        )
        diff = diff_revision(server, description_limit=LIMIT)
        assert {(r.method, r.path) for r in diff.orphaned_selection} == {
            ("POST", "/pets"),
            ("GET", "/pets/{petId}"),
        }
        assert {(r.method, r.path) for r in diff.orphaned_overrides} == {("POST", "/pets")}
        assert diff.orphaned_health_probe is not None

    def test_problems_that_would_block_acceptance(self):
        pending = _petstore()
        pending["paths"]["/pets"]["post"]["operationId"] = (
            "listPets!"  # unique, but normalizes to GET /pets' name
        )
        diff = diff_revision(_server(_petstore(), pending), description_limit=LIMIT)
        assert len(diff.problems) == 2

    def test_no_pending_revision(self):
        with pytest.raises(ValueError, match="pending"):
            diff_revision(_server(_petstore(), None), description_limit=LIMIT)

    def test_json_shape(self):
        pending = _petstore()
        del pending["paths"]["/pets/{petId}"]
        shaped = diff_revision(_server(_petstore(), pending), description_limit=LIMIT).to_json()
        assert shaped["removed"] == [{"method": "GET", "path": "/pets/{petId}"}]
        assert set(shaped) >= {
            "added",
            "removed",
            "changed",
            "servers",
            "orphaned_selection",
            "orphaned_overrides",
            "orphaned_health_probe",
            "problems",
        }


# --------------------------------------------------------------------------- accepting


class TestAccept:
    def test_choices_carry_over_and_new_operations_start_disabled(self):
        pending = _petstore()
        pending["paths"]["/owners"] = {
            "get": {"operationId": "listOwners", "responses": {"200": {"description": "ok"}}}
        }
        override = OpenAPIOverride(method="GET", path="/pets", name="all_pets")
        accepted, report = accept_revision(
            _server(_petstore(), pending, openapi_overrides=[override]), description_limit=LIMIT
        )
        assert accepted.openapi_snapshot.sha256 == "b" * 64 and accepted.openapi_pending is None
        assert {(r.method, r.path) for r in accepted.openapi_selection} == {
            ("GET", "/pets"),
            ("POST", "/pets"),
        }
        assert accepted.openapi_overrides == [override]
        assert report.dropped_selection == ()

    def test_orphans_are_dropped_and_reported(self):
        pending = _petstore()
        del pending["paths"]["/pets"]["post"]
        server = _server(
            _petstore(),
            pending,
            openapi_overrides=[OpenAPIOverride(method="POST", path="/pets", name="make_pet")],
            openapi_health_probe=OpenAPIOperationRef(method="POST", path="/pets"),
        )
        accepted, report = accept_revision(server, description_limit=LIMIT)
        assert [(r.method, r.path) for r in accepted.openapi_selection] == [("GET", "/pets")]
        assert accepted.openapi_overrides == [] and accepted.openapi_health_probe is None
        assert [(r.method, r.path) for r in report.dropped_selection] == [("POST", "/pets")]
        assert len(report.dropped_overrides) == 1 and report.dropped_health_probe is not None

    def test_an_unservable_result_is_refused_and_nothing_changes(self):
        pending = _petstore()
        pending["paths"]["/pets"]["post"]["operationId"] = "listPets!"
        server = _server(_petstore(), pending)
        with pytest.raises(MappingError):
            accept_revision(server, description_limit=LIMIT)
        assert server.openapi_snapshot.sha256 == "a" * 64

    def test_an_override_resolves_the_collision(self):
        pending = _petstore()
        pending["paths"]["/pets"]["post"]["operationId"] = "listPets!"
        server = _server(
            _petstore(),
            pending,
            openapi_overrides=[OpenAPIOverride(method="POST", path="/pets", name="createPet")],
        )
        accepted, _ = accept_revision(server, description_limit=LIMIT)
        assert accepted.openapi_snapshot.sha256 == "b" * 64


# --------------------------------------------------------------------------- routes


class SpecServer:
    """A real HTTP server on loopback whose spec the test can change."""

    def __init__(self) -> None:
        self.body = json.dumps(_petstore()).encode()
        self.hits = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.hits += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/openapi.json"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def serve(self, document: dict[str, Any]) -> None:
        self.body = json.dumps(document).encode()


@pytest.fixture
def spec_server():
    server = SpecServer()
    yield server
    server.httpd.shutdown()


@pytest.fixture
def client():
    settings = Settings(
        healthcheck=HealthCheckConfig(enabled=False),
        security=SecurityConfig(allow_private_networks=True),
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def _wait(client: TestClient, response: Any) -> dict[str, Any]:
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = client.get(f"/v1/openapi/imports/{job_id}").json()
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def _stored(client: TestClient) -> RegisteredServer:
    return client.portal.call(client.app.state.registry.get, "pets")  # type: ignore[union-attr]


def _import_from(client: TestClient, spec_server: SpecServer, **body: Any) -> dict[str, Any]:
    payload = {
        "id": "pets",
        "spec_url": spec_server.url,
        "base_url": BASE_URL,
        "selection": [{"method": "GET", "path": "/pets"}],
        **body,
    }
    return _wait(client, client.post("/v1/openapi/imports", json=payload))


class TestRoutes:
    def test_refresh_with_no_change_creates_nothing(self, client, spec_server):
        _import_from(client, spec_server)
        job = _wait(client, client.post("/v1/openapi/servers/pets/refresh"))
        assert job["outcome"] == "spec_unchanged"
        assert _stored(client).openapi_pending is None
        assert client.get("/v1/openapi/servers/pets/revision").status_code == 404

    def test_refresh_with_a_change_is_pending_until_accepted(self, client, spec_server):
        _import_from(client, spec_server)
        changed = _petstore()
        changed["paths"]["/pets"]["get"]["summary"] = "List some pets"
        spec_server.serve(changed)
        assert (
            _wait(client, client.post("/v1/openapi/servers/pets/refresh"))["outcome"]
            == "revision_pending"
        )
        # Live tools still come from the accepted snapshot.
        assert _stored(client).tools[0]["description"] == "List all pets"

        review = client.get("/v1/openapi/servers/pets/revision").json()
        assert review["diff"]["changed"] == [
            {"method": "GET", "path": "/pets", "changes": ["description"]}
        ]
        sha = review["pending"]["sha256"]

        accepted = client.post("/v1/openapi/servers/pets/revision/accept", json={"sha256": sha})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["tools"] == ["listPets"]
        stored = _stored(client)
        assert stored.openapi_pending is None and stored.openapi_snapshot.sha256 == sha
        assert stored.tools[0]["description"] == "List some pets"

    def test_reject_discards(self, client, spec_server):
        _import_from(client, spec_server)
        changed = _petstore()
        changed["info"]["title"] = "Changed"
        spec_server.serve(changed)
        _wait(client, client.post("/v1/openapi/servers/pets/refresh"))
        sha = client.get("/v1/openapi/servers/pets/revision").json()["pending"]["sha256"]
        before = _stored(client).openapi_snapshot.sha256
        response = client.post("/v1/openapi/servers/pets/revision/reject", json={"sha256": sha})
        assert response.status_code == 200
        stored = _stored(client)
        assert stored.openapi_pending is None and stored.openapi_snapshot.sha256 == before

    @pytest.mark.parametrize("action", ["accept", "reject"])
    def test_a_stale_sha_is_a_conflict(self, client, spec_server, action):
        _import_from(client, spec_server)
        changed = _petstore()
        changed["info"]["title"] = "Changed"
        spec_server.serve(changed)
        _wait(client, client.post("/v1/openapi/servers/pets/refresh"))
        response = client.post(
            f"/v1/openapi/servers/pets/revision/{action}", json={"sha256": "f" * 64}
        )
        assert response.status_code == 409
        assert _stored(client).openapi_pending is not None

    @pytest.mark.parametrize("action", ["accept", "reject"])
    def test_nothing_pending_is_404(self, client, spec_server, action):
        _import_from(client, spec_server)
        assert (
            client.post(
                f"/v1/openapi/servers/pets/revision/{action}", json={"sha256": "a" * 64}
            ).status_code
            == 404
        )

    def test_accept_that_cannot_be_served_is_refused(self, client, spec_server):
        _import_from(
            client,
            spec_server,
            selection=[{"method": "GET", "path": "/pets"}, {"method": "POST", "path": "/pets"}],
        )
        changed = _petstore()
        changed["paths"]["/pets"]["post"]["operationId"] = "listPets!"
        spec_server.serve(changed)
        _wait(client, client.post("/v1/openapi/servers/pets/refresh"))
        sha = client.get("/v1/openapi/servers/pets/revision").json()["pending"]["sha256"]
        response = client.post("/v1/openapi/servers/pets/revision/accept", json={"sha256": sha})
        assert response.status_code == 400
        assert len(response.json()["problems"]) == 2
        assert _stored(client).openapi_pending is not None

    def test_accept_reports_what_it_dropped(self, client, spec_server):
        _import_from(
            client,
            spec_server,
            selection=[{"method": "GET", "path": "/pets"}, {"method": "POST", "path": "/pets"}],
        )
        changed = _petstore()
        del changed["paths"]["/pets"]["post"]
        spec_server.serve(changed)
        _wait(client, client.post("/v1/openapi/servers/pets/refresh"))
        sha = client.get("/v1/openapi/servers/pets/revision").json()["pending"]["sha256"]
        body = client.post("/v1/openapi/servers/pets/revision/accept", json={"sha256": sha}).json()
        assert body["dropped"]["selection"] == [{"method": "POST", "path": "/pets"}]

    def test_reupload_creates_a_pending_revision_only_when_the_sha_differs(self, client):
        payload = {"id": "pets", "spec": json.dumps(_petstore()), "base_url": BASE_URL}
        _wait(client, client.post("/v1/openapi/imports", json=payload))
        reformatted = {**payload, "spec": json.dumps(_petstore(), indent=4)}
        assert (
            _wait(client, client.post("/v1/openapi/imports", json=reformatted))["outcome"]
            == "spec_unchanged"
        )
        changed = _petstore()
        changed["info"]["title"] = "Changed"
        assert (
            _wait(
                client,
                client.post("/v1/openapi/imports", json={**payload, "spec": json.dumps(changed)}),
            )["outcome"]
            == "revision_pending"
        )

    def test_refresh_needs_a_spec_url(self, client):
        _wait(
            client,
            client.post(
                "/v1/openapi/imports",
                json={"id": "pets", "spec": json.dumps(_petstore()), "base_url": BASE_URL},
            ),
        )
        response = client.post("/v1/openapi/servers/pets/refresh")
        assert response.status_code == 400
        assert "upload" in response.text

    def test_unknown_server(self, client):
        assert client.post("/v1/openapi/servers/nope/refresh").status_code == 404
        assert client.get("/v1/openapi/servers/nope/revision").status_code == 404


class TestManualOnly:
    async def test_no_background_path_fetches_a_spec(self, monkeypatch):
        # Detection is manual (ADR 0011 §2): neither the discovery poll nor the health
        # checker may re-fetch a spec URL, however long the hub runs.
        import mcp_hub.openapi.importer as importer
        import mcp_hub.openapi.isolation as isolation
        import mcp_hub.openapi.loader as loader
        from mcp_hub.config import HealthCheckConfig, TraceConfig
        from mcp_hub.health.checker import HealthChecker
        from mcp_hub.mcp.discovery import DiscoveryService
        from mcp_hub.registry.service import Registry
        from mcp_hub.storage.memory import InMemoryStorage
        from mcp_hub.trace.recorder import TraceRecorder

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a background path fetched a spec")

        for module in (importer, isolation, loader):
            for name in ("load_spec_from_url", "fetch_spec_bytes", "load_spec"):
                if hasattr(module, name):
                    monkeypatch.setattr(module, name, refuse)

        registry = Registry(InMemoryStorage())
        await registry.register(
            _server(
                _petstore(),
                None,
                openapi_snapshot=_snap(
                    _petstore(), "a" * 64, source_url="https://specs.example.com/p.json"
                ),
            )
        )
        await DiscoveryService(registry, openapi_description_limit=LIMIT).poll_once()
        await HealthChecker(
            registry, HealthCheckConfig(), TraceRecorder(), TraceConfig()
        ).check_all_once()
        assert (await registry.get("pets")).tools  # discovery did run, locally
