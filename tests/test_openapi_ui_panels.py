"""The servers page's panels for an OpenAPI server.

Invoke, Initialize, Playground and the capabilities view each call a backend's MCP
URL. An OpenAPI server's `url` is the synthetic `openapi:<id>` -- the hub is its MCP
server (ADR 0011) -- so these must be answered in-process by the virtual server, not
refused as "Invalid server URL" or sent over the network to `openapi:`.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import httpx
import pytest

from mcp_hub.app import create_app
from mcp_hub.config import HealthCheckConfig, SecurityConfig, Settings, StorageConfig
from mcp_hub.models import RegisteredServer
from mcp_hub.models.openapi import OpenAPIOperationRef, OpenAPISnapshot
from mcp_hub.openapi import executor
from mcp_hub.routes import ui_servers
from mcp_hub.routes.ui_invoke import keep_declared_strings

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "openapi"


def _server(**fields: Any) -> RegisteredServer:
    document = json.loads((FIXTURES / "petstore-3.1.json").read_text())
    values: dict[str, Any] = {
        "id": "pets",
        "url": "",
        "transport_kind": "openapi",
        "openapi_base_url": "https://petstore.example.com/v1",
        "openapi_snapshot": OpenAPISnapshot(document=document, dialect="3.1", sha256="b" * 64),
        "openapi_selection": [
            OpenAPIOperationRef(method="GET", path="/pets"),
            OpenAPIOperationRef(method="GET", path="/pets/{petId}"),
        ],
    }
    values.update(fields)
    return RegisteredServer(**values)


class Upstream:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/pets/404"):
            return httpx.Response(404, json={"code": 404, "message": "no such pet"})
        if request.url.path.startswith("/v1/pets/"):
            return httpx.Response(200, json={"id": 1, "name": "rex"})
        return httpx.Response(200, json=[{"id": 1, "name": "rex"}])


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def resolve(host: str, *, allow_private_networks: bool = False) -> str | None:
        return "93.184.216.34"

    monkeypatch.setattr(executor, "resolve_pinned_ip", resolve)


@pytest.fixture
async def hub():
    app = create_app(
        Settings(
            storage=StorageConfig(type="inmemory"),
            healthcheck=HealthCheckConfig(enabled=False),
            security=SecurityConfig(allow_private_networks=False),
        )
    )
    server = _server()
    await app.state.registry.register(server)
    upstream = Upstream()
    app.state.openapi_http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield app, client, upstream
    await app.state.openapi_http_client.aclose()


async def _stored(app: Any) -> RegisteredServer:
    server = await app.state.registry.get("pets")
    assert server is not None
    return server


class TestInvoke:
    async def test_a_tool_with_a_path_parameter(self, hub):
        app, client, upstream = hub
        response = await client.post("/ui/invoke/pets/showPetById", data={"petId": "1"})
        assert response.status_code == 200
        assert "Invalid server URL" not in response.text
        assert "rex" in response.text
        (request,) = upstream.requests
        assert str(request.url) == "https://petstore.example.com/v1/pets/1"

    async def test_an_upstream_error_shows_as_an_error(self, hub):
        _, client, _ = hub
        response = await client.post("/ui/invoke/pets/showPetById", data={"petId": "404"})
        assert "no such pet" in response.text
        assert "text-red-800" in response.text

    async def test_an_unselected_operation_is_refused_without_a_request(self, hub):
        _, client, upstream = hub
        response = await client.post("/ui/invoke/pets/createPet", data={"name": "x"})
        assert "text-red-800" in response.text and "Unknown tool" in response.text
        assert upstream.requests == []

    async def test_the_call_is_traced_with_the_path_template(self, hub):
        app, client, _ = hub
        await client.post("/ui/invoke/pets/showPetById", data={"petId": "1"})
        entries = app.state.trace_recorder.list("pets")
        operations = {entry.operation: entry for entry in entries}
        assert set(operations) == {"openapi_call", "tools/call"}
        # Not verbose: the template, never the expanded path.
        assert operations["openapi_call"].outbound_url.endswith("/pets/{petId}")
        assert operations["tools/call"].status == 200

    async def test_html_in_the_response_is_escaped(self, hub, monkeypatch):
        app, client, upstream = hub

        def evil(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"name": "<script>alert(1)</script>"})

        app.state.openapi_http_client = httpx.AsyncClient(transport=httpx.MockTransport(evil))
        response = await client.post("/ui/invoke/pets/showPetById", data={"petId": "1"})
        assert "<script>" not in response.text
        assert "&lt;script&gt;" in response.text


class TestInitialize:
    async def test_answered_by_the_hub(self, hub):
        _, client, upstream = hub
        response = await client.get("/ui/server/pets/initialize")
        assert response.status_code == 200
        assert "protocolVersion" in response.text
        assert "only http and https" not in response.text
        assert upstream.requests == []


class TestPlayground:
    async def test_tools_list(self, hub):
        _, client, _ = hub
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        response = await client.post("/ui/server/pets/playground", data={"request_body": body})
        assert response.status_code == 200
        assert "showPetById" in response.text
        assert "Could not connect" not in response.text

    async def test_tools_call(self, hub):
        _, client, upstream = hub
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "listPets", "arguments": {"limit": 5}},
            }
        )
        response = await client.post("/ui/server/pets/playground", data={"request_body": body})
        assert "rex" in response.text
        (request,) = upstream.requests
        assert request.url.params["limit"] == "5"


class TestCapabilities:
    async def test_live_view_lists_the_selection(self, hub):
        app, client, _ = hub
        server = await _stored(app)
        server.tools = []
        await app.state.registry.register(server)
        response = await client.get("/ui/server/pets/capabilities")
        assert response.status_code == 200
        assert "showPetById" in response.text and "listPets" in response.text
        assert "Handshake failed" not in response.text


class TestBackgroundProbe:
    async def test_openapi_servers_are_not_probed(self, monkeypatch):
        probed: list[str] = []

        async def probe(server_id: str, url: str, registry: Any, **_: Any) -> None:
            probed.append(server_id)

        monkeypatch.setattr(ui_servers, "_probe_server_mcp_info", probe)

        class Registry:
            async def list(self) -> list[RegisteredServer]:
                return [_server(mcp_transport="")]

        await ui_servers._probe_all_servers_task(Registry())  # type: ignore[arg-type]
        assert probed == []


class TestKeepDeclaredStrings:
    FORM = {"id": ["007"], "count": ["3"], "flag": ["true"]}
    GUESSED = {"id": 7, "count": 3, "flag": True}

    @pytest.mark.parametrize("declared", ["string", ["string"], ["string", "null"]])
    def test_a_string_property_gets_the_raw_text(self, declared):
        schema = {"properties": {"id": {"type": declared}, "count": {"type": "integer"}}}
        args = keep_declared_strings(self.GUESSED, self.FORM, schema)
        assert args == {"id": "007", "count": 3, "flag": True}

    @pytest.mark.parametrize("declared", [["string", "integer"], "integer", None])
    def test_otherwise_the_guess_stands(self, declared):
        schema = {"properties": {"id": {} if declared is None else {"type": declared}}}
        assert keep_declared_strings(self.GUESSED, self.FORM, schema) == self.GUESSED

    def test_no_schema(self):
        assert keep_declared_strings(self.GUESSED, self.FORM, {}) == self.GUESSED

    def test_the_input_is_not_mutated(self):
        guessed = dict(self.GUESSED)
        keep_declared_strings(guessed, self.FORM, {"properties": {"id": {"type": "string"}}})
        assert guessed == self.GUESSED
