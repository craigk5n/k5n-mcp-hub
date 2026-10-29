"""Story 13.6 -- the spec an OpenAPI server exposes as its one MCP resource.

Trimmed to the enabled operations (ADR 0011 §9): a caller learns about what it may
call and nothing else, including through components only a disabled operation used.
"""

from __future__ import annotations

import copy
import json
import pathlib

from mcp_hub.openapi.model import OperationKey
from mcp_hub.openapi.trimmed_spec import trimmed_spec

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "openapi"


def _petstore() -> dict:
    return json.loads((FIXTURES / "petstore-3.1.json").read_text())


class TestTrimmedSpec:
    def test_only_enabled_operations_remain(self):
        trimmed = trimmed_spec(_petstore(), "3.1", frozenset({OperationKey("GET", "/pets")}))
        assert list(trimmed["paths"]) == ["/pets"]
        assert list(trimmed["paths"]["/pets"]) == ["get"]
        assert "/pets/{petId}" not in json.dumps(trimmed)
        assert "createPet" not in json.dumps(trimmed)

    def test_components_only_a_disabled_operation_used_are_dropped(self):
        doc = _petstore()
        doc["components"]["schemas"]["SecretAdminThing"] = {
            "type": "object",
            "description": "internal",
        }
        doc["paths"]["/admin"] = {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/SecretAdminThing"}
                        }
                    }
                },
                "responses": {"200": {"description": "ok"}},
            }
        }
        trimmed = trimmed_spec(doc, "3.1", frozenset({OperationKey("GET", "/pets")}))
        text = json.dumps(trimmed)
        assert "SecretAdminThing" not in text and "/admin" not in text
        # What the enabled operation reaches, transitively, stays.
        assert set(trimmed["components"]["schemas"]) == {"Pet", "NewPet", "Error"}
        assert set(trimmed["components"]["responses"]) == {"Error"}

    def test_servers_extensions_and_webhooks_are_dropped(self):
        doc = _petstore()
        doc["x-internal"] = "secret"
        doc["webhooks"] = {"w": {"post": {"responses": {"200": {"description": "ok"}}}}}
        doc["paths"]["/pets"]["x-owner"] = "team-pets"
        trimmed = trimmed_spec(doc, "3.1", frozenset({OperationKey("GET", "/pets")}))
        assert (
            "servers" not in trimmed and "x-internal" not in trimmed and "webhooks" not in trimmed
        )
        assert "petstore.example.com" not in json.dumps(trimmed)
        assert trimmed["info"]["title"] == "Petstore"

    def test_only_tags_in_use_remain(self):
        doc = _petstore()
        doc["tags"] = [{"name": "pets"}, {"name": "admin", "description": "the /admin routes"}]
        doc["paths"]["/pets"]["get"]["tags"] = ["pets"]
        trimmed = trimmed_spec(doc, "3.1", frozenset({OperationKey("GET", "/pets")}))
        assert trimmed["tags"] == [{"name": "pets"}]

    def test_path_level_parameters_are_kept_for_a_kept_path(self):
        doc = _petstore()
        doc["paths"]["/pets"]["parameters"] = [
            {"name": "X-Trace", "in": "header", "schema": {"type": "string"}}
        ]
        trimmed = trimmed_spec(doc, "3.1", frozenset({OperationKey("GET", "/pets")}))
        assert trimmed["paths"]["/pets"]["parameters"][0]["name"] == "X-Trace"

    def test_nothing_selected_is_an_empty_paths_object(self):
        trimmed = trimmed_spec(_petstore(), "3.1", frozenset())
        assert trimmed["paths"] == {} and "components" not in trimmed

    def test_swagger_2_top_level_containers(self):
        doc = json.loads((FIXTURES / "swagger2-kitchen-sink.json").read_text())
        trimmed = trimmed_spec(doc, "2.0", frozenset({OperationKey("GET", "/pets/{petId}")}))
        assert set(trimmed["paths"]) == {"/pets/{petId}"}
        assert set(trimmed["definitions"]) == {"Pet", "NewPet", "Error"}
        assert set(trimmed["responses"]) == {"NotFound"}
        assert "host" not in trimmed and "basePath" not in trimmed
        assert "parameters" not in trimmed  # limitParam belonged to GET /pets only
        assert "uploadPhoto" not in json.dumps(trimmed)

    def test_input_is_not_modified(self):
        doc = _petstore()
        before = copy.deepcopy(doc)
        trimmed = trimmed_spec(doc, "3.1", frozenset({OperationKey("GET", "/pets")}))
        trimmed["info"]["title"] = "changed"
        assert doc == before

    def test_path_item_refs_are_inlined(self):
        doc = _petstore()
        doc["x-items"] = {"shared": {"get": {"responses": {"200": {"description": "ok"}}}}}
        doc["paths"]["/shared"] = {"$ref": "#/x-items/shared"}
        trimmed = trimmed_spec(doc, "3.1", frozenset({OperationKey("GET", "/shared")}))
        assert trimmed["paths"]["/shared"]["get"]["responses"]["200"]["description"] == "ok"
        assert "x-items" not in trimmed


class TestReviewFixes:
    """Security review of Story 13.6, finding 1: what the docstring promised."""

    def _leaky(self) -> dict:
        return {
            "openapi": "3.0.3",
            "info": {
                "title": "t",
                "version": "1",
                "description": "see http://intranet.corp/wiki",
                "contact": {"email": "ops@corp"},
                "x-internal": 1,
            },
            "paths": {
                "/a": {
                    "get": {
                        "operationId": "getA",
                        "servers": [{"url": "http://internal.corp:8443"}],
                        "x-owner": "bob",
                        "security": [{"hidden": []}],
                        "callbacks": {
                            "cb": {
                                "{$request.body#/url}": {
                                    "post": {"responses": {"200": {"description": "ok"}}}
                                }
                            }
                        },
                        "responses": {
                            "200": {
                                "description": "ok",
                                "links": {
                                    "l": {
                                        "operationRef": "#/paths/~1admin/delete",
                                        "operationId": "deleteAdmin",
                                    }
                                },
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/A"}
                                    }
                                },
                            }
                        },
                    }
                },
                "/admin": {
                    "delete": {
                        "operationId": "deleteAdmin",
                        "responses": {"200": {"description": "ok"}},
                    }
                },
            },
            "components": {
                "schemas": {
                    "A": {
                        "type": "object",
                        "x-ext": "secret",
                        "properties": {
                            "x-header-name": {"type": "string"},
                            "next": {"$ref": "#/paths/~1admin/delete"},
                        },
                    }
                }
            },
        }

    def test_nothing_the_docstring_removes_survives(self):
        out = json.dumps(trimmed_spec(self._leaky(), "3.0", frozenset({OperationKey("GET", "/a")})))
        for leak in [
            "internal.corp",
            "x-owner",
            "hidden",
            "deleteAdmin",
            "/admin",
            "intranet",
            "ops@corp",
            "x-internal",
            "x-ext",
            "callbacks",
            "$request",
        ]:
            assert leak not in out, leak

    def test_what_mapping_needs_is_kept(self):
        out = trimmed_spec(self._leaky(), "3.0", frozenset({OperationKey("GET", "/a")}))
        op = out["paths"]["/a"]["get"]
        assert op["operationId"] == "getA"
        assert op["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/A"
        }
        assert out["info"] == {"title": "t", "version": "1"}
        # A property *named* x-header-name is a name, not an extension.
        assert "x-header-name" in out["components"]["schemas"]["A"]["properties"]

    def test_swagger_2_operation_fields(self):
        doc = json.loads((FIXTURES / "swagger2-kitchen-sink.json").read_text())
        doc["paths"]["/pets"]["get"]["schemes"] = ["http"]
        doc["paths"]["/pets"]["get"]["security"] = [{"oauth": []}]
        out = trimmed_spec(doc, "2.0", frozenset({OperationKey("GET", "/pets")}))
        op = out["paths"]["/pets"]["get"]
        assert "schemes" not in op and "security" not in op and "x-internal-owner" not in op
        assert op["operationId"] == "listPets"
