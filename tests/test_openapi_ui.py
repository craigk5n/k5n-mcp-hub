"""Story 13.8 -- the admin UI for OpenAPI servers, and the API it needs.

Import is spec-first (decided 2026-10-29): importing without a base URL creates a
draft with nothing exposed; the admin then confirms a base URL pre-filled from the
spec's `servers[]`, through `PATCH /v1/openapi/servers/{id}/settings`. Tools cannot be
enabled until a base URL is set.

Every piece of spec text reaches the page through autoescaping, which a test checks
with a spec full of markup.
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import HealthCheckConfig, SecurityConfig, Settings
from mcp_hub.models.openapi import OpenAPISnapshot
from mcp_hub.openapi.hints import operation_tags, security_scheme_hints, suggested_base_urls

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "openapi"
BASE_URL = "http://127.0.0.1:9/v1"


def _petstore() -> dict[str, Any]:
    return json.loads((FIXTURES / "petstore-3.1.json").read_text())


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


def _import(
    client: TestClient, document: dict[str, Any] | None = None, **body: Any
) -> dict[str, Any]:
    payload = {"id": "pets", "spec": json.dumps(document or _petstore()), **body}
    return _wait(client, client.post("/v1/openapi/imports", json=payload))


def _stored(client: TestClient) -> Any:
    return client.portal.call(client.app.state.registry.get, "pets")  # type: ignore[union-attr]


# --------------------------------------------------------------------------- hints


def _snap(document: dict[str, Any], dialect: str = "3.1", source_url: str = "") -> OpenAPISnapshot:
    return OpenAPISnapshot(
        document=document, dialect=dialect, sha256="a" * 64, source_url=source_url
    )


class TestHints:
    def test_servers_with_variables_take_their_defaults(self):
        doc = _petstore()
        doc["servers"] = [
            {
                "url": "https://{region}.api.example.com/{version}",
                "variables": {
                    "region": {"default": "eu"},
                    "version": {"default": "v2", "enum": ["v1", "v2"]},
                },
            },
            {"url": "https://static.example.com"},
        ]
        assert suggested_base_urls(_snap(doc)) == [
            "https://eu.api.example.com/v2",
            "https://static.example.com",
        ]

    def test_relative_servers_resolve_against_where_the_spec_came_from(self):
        doc = _petstore()
        doc["servers"] = [{"url": "/api/v3"}]
        assert suggested_base_urls(
            _snap(doc, source_url="https://petstore.example.com/openapi.json")
        ) == ["https://petstore.example.com/api/v3"]
        # An uploaded spec has nothing to resolve against: offer what it says.
        assert suggested_base_urls(_snap(doc)) == ["/api/v3"]

    def test_swagger_2(self):
        doc = json.loads((FIXTURES / "swagger2-kitchen-sink.json").read_text())
        assert suggested_base_urls(_snap(doc, "2.0")) == [
            "https://api.example.com/v2",
            "http://api.example.com/v2",
        ]

    def test_security_scheme_hints_map_to_the_hubs_auth_fields(self):
        doc = json.loads((FIXTURES / "swagger2-kitchen-sink.json").read_text())
        hints = {h["name"]: h for h in security_scheme_hints(_snap(doc, "2.0"))}
        assert hints["apiKey"]["suggest"] == {
            "auth_type": "api_key",
            "api_key_name": "X-API-Key",
            "api_key_in": "header",
        }
        assert hints["basicAuth"]["suggest"] == {"auth_type": "basic"}
        assert hints["oauth"]["suggest"]["auth_type"] == "oauth"
        assert hints["oauth"]["suggest"]["oauth_token_url"] == "https://auth.example.com/token"

    def test_http_bearer_and_cookie_keys(self):
        doc = _petstore()
        doc["components"]["securitySchemes"] = {
            "b": {"type": "http", "scheme": "bearer"},
            "c": {"type": "apiKey", "in": "cookie", "name": "sid"},
        }
        hints = {h["name"]: h for h in security_scheme_hints(_snap(doc))}
        assert hints["b"]["suggest"] == {"auth_type": "bearer"}
        assert hints["c"]["suggest"] is None  # the hub never sends cookies

    def test_operation_tags(self):
        doc = _petstore()
        doc["paths"]["/pets"]["get"]["tags"] = ["pets", "read"]
        from mcp_hub.openapi.model import OperationKey

        tags = operation_tags(_snap(doc))
        assert tags[OperationKey("GET", "/pets")] == ["pets", "read"]
        assert tags[OperationKey("POST", "/pets")] == []


# --------------------------------------------------------------------------- draft servers and settings


class TestDraftAndSettings:
    def test_import_without_a_base_url_is_a_draft(self, client):
        job = _import(client)
        assert job["status"] == "succeeded", job
        server = _stored(client)
        assert server.openapi_base_url == "" and server.openapi_selection == []

    def test_a_draft_cannot_enable_tools_or_a_probe(self, client):
        _import(client)
        for body in (
            {"selection": [{"method": "GET", "path": "/pets"}]},
            {"health_probe": {"method": "GET", "path": "/pets"}},
        ):
            response = client.put("/v1/openapi/servers/pets/tools", json=body)
            assert response.status_code == 400
            assert "base URL" in json.dumps(response.json())

    def test_settings_confirm_the_base_url_then_tools_can_be_enabled(self, client):
        _import(client)
        response = client.patch("/v1/openapi/servers/pets/settings", json={"base_url": BASE_URL})
        assert response.status_code == 200, response.text
        assert _stored(client).openapi_base_url == BASE_URL
        assert (
            client.put(
                "/v1/openapi/servers/pets/tools",
                json={"selection": [{"method": "GET", "path": "/pets"}]},
            ).status_code
            == 200
        )

    def test_settings_keep_what_is_omitted(self, client):
        _import(client, base_url=BASE_URL, auth_type="bearer", bearer_token="t1", name="Pets")
        client.patch("/v1/openapi/servers/pets/settings", json={"description": "The pet API"})
        server = _stored(client)
        assert (server.bearer_token, server.name, server.description, server.openapi_base_url) == (
            "t1",
            "Pets",
            "The pet API",
            BASE_URL,
        )

    @pytest.mark.parametrize(
        "body, fragment",
        [
            ({"base_url": "http://user:pw@127.0.0.1:9/v1"}, "credentials"),
            ({"base_url": "ftp://127.0.0.1/x"}, "base_url"),
            ({"auth_type": "api_key", "api_key_in": "header"}, "api_key_name"),
            ({"spec": "{}"}, "spec"),
        ],
    )
    def test_settings_are_validated_like_an_import(self, client, body, fragment):
        _import(client)
        response = client.patch("/v1/openapi/servers/pets/settings", json=body)
        assert response.status_code == 400
        assert fragment in response.text

    def test_settings_base_url_goes_through_the_ssrf_guard(self):
        settings = Settings(
            healthcheck=HealthCheckConfig(enabled=False),
            security=SecurityConfig(allow_private_networks=False),
        )
        with TestClient(create_app(settings)) as client:
            _import(client)
            response = client.patch(
                "/v1/openapi/servers/pets/settings", json={"base_url": "http://169.254.169.254/x"}
            )
        assert response.status_code == 400

    def test_settings_on_an_unknown_server(self, client):
        assert client.patch("/v1/openapi/servers/nope/settings", json={}).status_code == 404

    def test_operations_carry_hints_and_tags(self, client):
        doc = _petstore()
        doc["paths"]["/pets"]["get"]["tags"] = ["pets"]
        _import(client, doc)
        body = client.get("/v1/openapi/servers/pets/operations").json()
        assert body["suggested_base_urls"] == ["https://petstore.example.com/v1"]
        assert body["security_schemes"] == []
        by_key = {(o["method"], o["path"]): o for o in body["operations"]}
        assert by_key[("GET", "/pets")]["tags"] == ["pets"]

    async def test_the_executor_refuses_a_server_with_no_base_url(self):
        from mcp_hub.auth.caller import SERVICE_IDENTITY
        from mcp_hub.models import RegisteredServer
        from mcp_hub.openapi.executor import CallLimits, call_tool
        from mcp_hub.openapi.mapping import build_tools
        from mcp_hub.openapi.model import OperationKey

        (tool,) = build_tools(
            _petstore(),
            "3.1",
            selection=frozenset({OperationKey("GET", "/pets")}),
            overrides={},
            description_limit=1024,
        )
        server = RegisteredServer(id="p", url="", transport_kind="openapi")
        limits = CallLimits(
            timeout_seconds=1, max_response_bytes=10, max_redirects=0, allow_private_networks=False
        )
        result = await call_tool(tool, {}, server=server, caller=SERVICE_IDENTITY, limits=limits)
        assert result.is_error and "base URL" in result.content[0]["text"]


# --------------------------------------------------------------------------- pages


class TestPages:
    def test_index_lists_openapi_servers_and_offers_import(self, client):
        _import(client, base_url=BASE_URL)
        page = client.get("/ui/openapi")
        assert page.status_code == 200
        assert 'href="/ui/openapi/pets"' in page.text
        assert 'id="openapi-import-form"' in page.text

    def test_job_partial_polls_until_done(self, client):
        response = client.post(
            "/v1/openapi/imports", json={"id": "pets", "spec": json.dumps(_petstore())}
        )
        job_id = response.json()["job_id"]
        _wait(client, response)
        done = client.get(f"/ui/openapi/jobs/{job_id}")
        assert done.status_code == 200
        assert 'href="/ui/openapi/pets"' in done.text
        assert "every 1s" not in done.text  # finished: no more polling

    def test_job_partial_shows_the_error_and_pointer(self, client):
        doc = _petstore()
        doc["info"]["title"] = 7
        response = client.post("/v1/openapi/imports", json={"id": "pets", "spec": json.dumps(doc)})
        job = _wait(client, response)
        page = client.get(f"/ui/openapi/jobs/{job['job_id']}").text
        assert "/info/title" in page

    def test_unknown_job_partial(self, client):
        assert client.get("/ui/openapi/jobs/nope").status_code == 404

    def test_manage_page_for_a_draft_asks_for_a_base_url(self, client):
        _import(client)
        page = client.get("/ui/openapi/pets").text
        assert "https://petstore.example.com/v1" in page  # the suggestion
        assert 'id="openapi-base-url"' in page
        assert "confirm" in page.lower()

    def test_manage_page_groups_operations_by_tag_and_marks_them(self, client):
        doc = _petstore()
        doc["paths"]["/pets"]["get"]["tags"] = ["read"]
        doc["paths"]["/pets"]["post"]["parameters"] = [
            {"name": "Authorization", "in": "header", "schema": {"type": "string"}}
        ]
        doc["paths"]["/upload"] = {
            "post": {
                "requestBody": {"content": {"multipart/form-data": {"schema": {"type": "object"}}}},
                "responses": {"200": {"description": "ok"}},
            }
        }
        _import(client, doc, base_url=BASE_URL)
        page = client.get("/ui/openapi/pets").text
        assert "read" in page and "untagged" in page.lower()
        assert "List all pets" in page  # the description as the model sees it
        assert "multipart/form-data" in page  # unsupported reason
        assert "Authorization" in page  # dropped header
        assert 'data-path="/pets/{petId}"' in page

    def test_probe_picker_lists_only_eligible_gets(self, client):
        _import(client, base_url=BASE_URL)
        page = client.get("/ui/openapi/pets").text
        start = page.index('id="openapi-probe"')
        picker = page[start : page.index("</select>", start)]
        assert 'value=""' in picker  # "none", the default
        assert "GET /pets" in picker
        assert "/pets/{petId}" not in picker  # has a required parameter
        assert "POST" not in picker

    def test_security_schemes_are_offered_as_suggestions(self, client):
        doc = _petstore()
        doc["components"]["securitySchemes"] = {
            "key": {"type": "apiKey", "in": "header", "name": "X-Pet-Key"}
        }
        _import(client, doc, base_url=BASE_URL)
        page = client.get("/ui/openapi/pets").text
        assert "X-Pet-Key" in page

    def test_check_for_updates_needs_a_source_url(self, client):
        _import(client, base_url=BASE_URL)
        page = client.get("/ui/openapi/pets").text
        # Uploaded: the refresh button is replaced by a hint to re-upload.
        assert 'id="openapi-refresh"' not in page

    def test_revision_page(self, client):
        _import(client, base_url=BASE_URL)
        changed = _petstore()
        del changed["paths"]["/pets/{petId}"]
        _import(client, changed)
        page = client.get("/ui/openapi/pets/revision")
        assert page.status_code == 200
        assert "/pets/{petId}" in page.text  # listed as removed
        assert 'id="openapi-accept"' in page.text and 'id="openapi-reject"' in page.text
        assert client.get("/ui/openapi/pets").text.count("/ui/openapi/pets/revision") >= 1

    def test_revision_page_without_a_revision(self, client):
        _import(client, base_url=BASE_URL)
        assert client.get("/ui/openapi/pets/revision").status_code == 404

    @pytest.mark.parametrize("route", ["/ui/openapi/nope", "/ui/openapi/nope/revision"])
    def test_unknown_server(self, client, route):
        assert client.get(route).status_code == 404

    def test_servers_page_links_here(self, client):
        _import(client, base_url=BASE_URL)
        page = client.get("/ui/servers").text
        assert 'href="/ui/openapi"' in page
        assert 'href="/ui/openapi/pets"' in page


class TestEscaping:
    def test_spec_markup_is_never_rendered_as_html(self, client):
        doc = _petstore()
        evil = "<script>alert('x')</script>"
        doc["info"]["title"] = evil
        doc["paths"]["/pets"]["get"]["summary"] = "<img src=x onerror=alert(1)>"
        doc["paths"]["/pets"]["get"]["description"] = evil
        doc["paths"]["/pets"]["get"]["tags"] = ["<b>tag</b>"]
        doc["paths"]["/pets"]["get"]["parameters"][0]["description"] = evil
        doc["servers"] = [{"url": 'https://x.example.com/"><script>alert(2)</script>'}]
        doc.setdefault("components", {})["securitySchemes"] = {
            "k": {"type": "apiKey", "in": "header", "name": "<i>X</i>"}
        }
        _import(client, doc, base_url=BASE_URL)
        _import(client, {**doc, "x-changed": True})  # a pending revision, for the revision page
        for route in ("/ui/openapi", "/ui/openapi/pets", "/ui/openapi/pets/revision"):
            page = client.get(route).text
            assert "<script>alert" not in page, route
            assert "<img src=x" not in page, route
            assert "<b>tag</b>" not in page and "<i>X</i>" not in page, route
        assert "&lt;script&gt;" in client.get("/ui/openapi/pets").text


class TestAdminOnly:
    @pytest.fixture
    def jwt_client(self):
        from tests.test_multitenant_integration import AUDIENCE, ISSUER, JWKS_URI, jwks_client
        from mcp_hub.config import AuthConfig, JWTAuthConfig

        settings = Settings(
            healthcheck=HealthCheckConfig(enabled=False),
            security=SecurityConfig(allow_private_networks=True),
            auth=AuthConfig(
                type="jwt", jwt=JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, jwks_uri=JWKS_URI)
            ),
        )
        app = create_app(settings)
        app.state.authenticator._client = jwks_client()  # type: ignore[attr-defined]
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client

    @pytest.mark.parametrize(
        "route",
        ["/ui/openapi", "/ui/openapi/pets", "/ui/openapi/pets/revision", "/ui/openapi/jobs/x"],
    )
    def test_non_admin_is_refused(self, jwt_client, route):
        from tests.test_multitenant_integration import token_for

        headers = {"Authorization": f"Bearer {token_for('bob', 'files:use')}"}
        assert jwt_client.get(route, headers=headers).status_code == 403

    def test_settings_need_admin(self, jwt_client):
        from tests.test_multitenant_integration import token_for

        headers = {"Authorization": f"Bearer {token_for('bob', 'files:use')}"}
        assert (
            jwt_client.patch(
                "/v1/openapi/servers/pets/settings", json={}, headers=headers
            ).status_code
            == 403
        )
