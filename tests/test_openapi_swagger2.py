"""Story 13.2 -- Swagger 2.0 normalized to the OpenAPI 3.0 shape mapping consumes.

The kitchen-sink fixture is checked against a hand-written 3.0 expectation, so every
conversion decision is visible in one diffable file. The targeted tests below pin
the rules one at a time, including the "unsupported, not fatal" cases (ADR 0011 §6:
an operation the hub cannot represent is shown but cannot be enabled; the rest of
the import still works).
"""

from __future__ import annotations

import copy
import json
import pathlib
from typing import Any

import pytest

from mcp_hub.openapi.loader import load_spec_unsandboxed
from mcp_hub.openapi.swagger2 import UNSUPPORTED, swagger2_to_openapi3

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "openapi"
MAX = 5 * 1024 * 1024


def _fixture_json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _swagger(**extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "swagger": "2.0",
        "info": {"title": "t", "version": "1"},
        "paths": {},
    }
    doc.update(extra)
    return doc


def _one_op(operation: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """A 2.0 document with `operation` as POST /things/{id}, converted."""
    operation = {"responses": {"200": {"description": "ok"}}, **operation}
    doc = _swagger(paths={"/things/{id}": {"post": operation}}, **extra)
    return swagger2_to_openapi3(doc)["paths"]["/things/{id}"]["post"]


def _param(**fields: Any) -> dict[str, Any]:
    return {"name": "p", "in": "query", **fields}


def _reload(converted: dict[str, Any]) -> Any:
    """The converted document through the real loader: valid 3.0, refs intact."""
    return load_spec_unsandboxed(json.dumps(converted).encode(), max_bytes=MAX)


# --------------------------------------------------------------------------- whole documents


class TestKitchenSink:
    def test_matches_the_hand_written_expectation(self):
        source = _fixture_json("swagger2-kitchen-sink.json")
        expected = _fixture_json("swagger2-kitchen-sink.expected-3.0.json")
        assert swagger2_to_openapi3(source) == expected

    @pytest.mark.parametrize("name", ["swagger2-kitchen-sink.json", "petstore-swagger2.json"])
    def test_source_loads_and_output_is_valid_openapi_30(self, name):
        raw = (FIXTURES / name).read_bytes()
        loaded = load_spec_unsandboxed(raw, max_bytes=MAX)
        assert loaded.dialect == "2.0"
        reloaded = _reload(swagger2_to_openapi3(loaded.document))
        assert reloaded.dialect == "3.0"

    def test_input_is_not_modified(self):
        source = _fixture_json("swagger2-kitchen-sink.json")
        before = copy.deepcopy(source)
        swagger2_to_openapi3(source)
        assert source == before

    def test_output_shares_nothing_with_the_input(self):
        source = _fixture_json("swagger2-kitchen-sink.json")
        converted = swagger2_to_openapi3(source)
        converted["info"]["title"] = "changed"
        converted["paths"]["/pets"]["get"]["tags"].append("changed")
        assert source["info"]["title"] == "Kitchen sink"
        assert source["paths"]["/pets"]["get"]["tags"] == ["pets"]

    def test_refuses_a_non_swagger_document(self):
        with pytest.raises(ValueError, match="2.0"):
            swagger2_to_openapi3({"openapi": "3.0.3", "info": {}, "paths": {}})

    def test_top_level_extensions_and_external_docs_survive(self):
        doc = _swagger(**{"x-logo": {"url": "l.png"}, "externalDocs": {"url": "https://d"}})
        converted = swagger2_to_openapi3(doc)
        assert converted["x-logo"] == {"url": "l.png"}
        assert converted["externalDocs"] == {"url": "https://d"}


# --------------------------------------------------------------------------- servers


class TestServers:
    @pytest.mark.parametrize(
        "fields, servers",
        [
            (
                {"host": "h.example", "basePath": "/v1", "schemes": ["https"]},
                ["https://h.example/v1"],
            ),
            (
                {"host": "h.example", "schemes": ["http", "https"]},
                ["http://h.example", "https://h.example"],
            ),
            ({"host": "h.example", "basePath": "/", "schemes": ["https"]}, ["https://h.example"]),
            # No schemes: the spec's own scheme applies, which only the importer knows.
            # A protocol-relative URL says exactly that, rather than guessing https.
            ({"host": "h.example", "basePath": "/v1"}, ["//h.example/v1"]),
            ({"basePath": "/v1"}, ["/v1"]),
            # ws/wss are not REST; nothing else to offer means no suggestion at all.
            ({"host": "h.example", "schemes": ["ws", "wss"]}, None),
            ({}, None),
        ],
    )
    def test_suggestion(self, fields, servers):
        converted = swagger2_to_openapi3(_swagger(**fields))
        if servers is None:
            assert "servers" not in converted
        else:
            assert [s["url"] for s in converted["servers"]] == servers


# --------------------------------------------------------------------------- parameters


class TestParameters:
    @pytest.mark.parametrize(
        "location, fmt, style, explode",
        [
            ("query", None, "form", False),  # csv is the 2.0 default
            ("query", "csv", "form", False),
            ("query", "multi", "form", True),
            ("query", "ssv", "spaceDelimited", False),
            ("query", "pipes", "pipeDelimited", False),
            ("header", "csv", "simple", False),
            ("path", "csv", "simple", False),
        ],
    )
    def test_collection_formats(self, location, fmt, style, explode):
        param = _param(**{"in": location, "type": "array", "items": {"type": "string"}})
        if location == "path":
            param = {**param, "name": "id", "required": True}
        if fmt:
            param["collectionFormat"] = fmt
        converted = _one_op({"parameters": [param]})["parameters"][0]
        assert (converted["style"], converted["explode"]) == (style, explode)
        assert "collectionFormat" not in converted["schema"]
        assert UNSUPPORTED not in _one_op({"parameters": [param]})

    def test_tsv_has_no_30_equivalent(self):
        param = _param(type="array", items={"type": "string"}, collectionFormat="tsv")
        op = _one_op({"parameters": [param]})
        assert any("tsv" in reason for reason in op[UNSUPPORTED])

    def test_a_format_that_only_query_allows_is_unsupported_elsewhere(self):
        param = _param(
            **{
                "in": "header",
                "type": "array",
                "items": {"type": "string"},
                "collectionFormat": "multi",
            }
        )
        op = _one_op({"parameters": [param]})
        assert any("multi" in reason for reason in op[UNSUPPORTED])

    def test_nested_arrays_are_unsupported(self):
        param = _param(type="array", items={"type": "array", "items": {"type": "string"}})
        op = _one_op({"parameters": [param]})
        assert any("nested" in reason for reason in op[UNSUPPORTED])

    def test_validation_keywords_move_into_the_schema(self):
        param = _param(
            type="string",
            format="uuid",
            default="x",
            enum=["x", "y"],
            pattern="^[xy]$",
            minLength=1,
            maxLength=1,
            description="A thing",
            required=True,
            allowEmptyValue=True,
            **{"x-extra": 1},
        )
        converted = _one_op({"parameters": [param]})["parameters"][0]
        assert converted == {
            "name": "p",
            "in": "query",
            "description": "A thing",
            "required": True,
            "allowEmptyValue": True,
            "x-extra": 1,
            "schema": {
                "type": "string",
                "format": "uuid",
                "default": "x",
                "enum": ["x", "y"],
                "pattern": "^[xy]$",
                "minLength": 1,
                "maxLength": 1,
            },
        }

    def test_operation_parameter_overrides_path_parameter(self):
        doc = _swagger(
            paths={
                "/a": {
                    "parameters": [
                        {"name": "q", "in": "query", "type": "string"},
                        {"name": "keep", "in": "query", "type": "string"},
                    ],
                    "get": {
                        "parameters": [
                            {"name": "q", "in": "query", "type": "integer"},
                            {"name": "q", "in": "header", "type": "string"},
                        ],
                        "responses": {"200": {"description": "ok"}},
                    },
                }
            }
        )
        converted = swagger2_to_openapi3(doc)
        item = converted["paths"]["/a"]
        assert "parameters" not in item  # merged into each operation
        params = [(p["name"], p["in"], p["schema"]["type"]) for p in item["get"]["parameters"]]
        assert params == [
            ("q", "query", "integer"),
            ("keep", "query", "string"),
            ("q", "header", "string"),
        ]

    def test_global_parameter_refs_are_inlined(self):
        doc = _swagger(
            parameters={"Id": {"name": "id", "in": "path", "required": True, "type": "string"}},
            paths={
                "/a/{id}": {
                    "get": {
                        "parameters": [{"$ref": "#/parameters/Id"}],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        )
        converted = swagger2_to_openapi3(doc)
        assert converted["paths"]["/a/{id}"]["get"]["parameters"] == [
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
        ]
        assert "parameters" not in converted.get("components", {})
        assert _reload(converted).dialect == "3.0"


# --------------------------------------------------------------------------- bodies


class TestBodies:
    def test_body_with_several_consumes_gets_each_media_type(self):
        op = _one_op(
            {
                "consumes": ["application/json", "application/xml"],
                "parameters": [{"name": "b", "in": "body", "schema": {"type": "object"}}],
            }
        )
        assert op["requestBody"] == {
            "content": {
                "application/json": {"schema": {"type": "object"}},
                "application/xml": {"schema": {"type": "object"}},
            }
        }

    def test_body_defaults_to_json_without_consumes(self):
        op = _one_op({"parameters": [{"name": "b", "in": "body", "schema": {"type": "object"}}]})
        assert list(op["requestBody"]["content"]) == ["application/json"]

    def test_operation_consumes_overrides_global(self):
        op = _one_op(
            {
                "consumes": ["application/xml"],
                "parameters": [{"name": "b", "in": "body", "schema": {"type": "object"}}],
            },
            consumes=["application/json"],
        )
        assert list(op["requestBody"]["content"]) == ["application/xml"]

    def test_form_defaults_to_urlencoded_without_consumes(self):
        op = _one_op({"parameters": [{"name": "f", "in": "formData", "type": "string"}]})
        assert list(op["requestBody"]["content"]) == ["application/x-www-form-urlencoded"]
        assert "required" not in op["requestBody"]  # no required field, no required body

    def test_form_prefers_urlencoded_when_both_are_offered(self):
        op = _one_op(
            {
                "consumes": ["multipart/form-data", "application/x-www-form-urlencoded"],
                "parameters": [{"name": "f", "in": "formData", "type": "string"}],
            }
        )
        assert list(op["requestBody"]["content"]) == ["application/x-www-form-urlencoded"]

    def test_multipart_only_form_without_files_is_left_for_mapping(self):
        # The media type alone is mapping's call (Story 13.3 marks non-JSON/form
        # bodies); the normalizer only marks what it could not convert.
        op = _one_op(
            {
                "consumes": ["multipart/form-data"],
                "parameters": [{"name": "f", "in": "formData", "type": "string"}],
            }
        )
        assert list(op["requestBody"]["content"]) == ["multipart/form-data"]
        assert UNSUPPORTED not in op

    def test_form_array_keeps_its_collection_format_as_encoding(self):
        op = _one_op(
            {
                "parameters": [
                    {
                        "name": "ids",
                        "in": "formData",
                        "type": "array",
                        "items": {"type": "integer"},
                        "collectionFormat": "multi",
                    }
                ]
            }
        )
        media = op["requestBody"]["content"]["application/x-www-form-urlencoded"]
        assert media["encoding"] == {"ids": {"style": "form", "explode": True}}

    def test_body_and_form_in_one_operation_is_unsupported(self):
        # Invalid in 2.0, but the 2.0 validator does not catch it, and one request
        # cannot have both bodies.
        op = _one_op(
            {
                "parameters": [
                    {"name": "b", "in": "body", "schema": {"type": "object"}},
                    {"name": "f", "in": "formData", "type": "string"},
                ]
            }
        )
        assert any("body" in reason and "formData" in reason for reason in op[UNSUPPORTED])


# --------------------------------------------------------------------------- responses


class TestResponses:
    def test_response_without_schema_has_no_content(self):
        op = _one_op({"responses": {"204": {"description": "gone"}}})
        assert op["responses"] == {"204": {"description": "gone"}}

    def test_examples_follow_their_media_type(self):
        op = _one_op(
            {
                "produces": ["application/json", "text/plain"],
                "responses": {
                    "200": {
                        "description": "ok",
                        "schema": {"type": "string"},
                        "examples": {"application/json": '"hi"', "text/csv": "a,b"},
                    }
                },
            }
        )
        content = op["responses"]["200"]["content"]
        assert content["application/json"]["example"] == '"hi"'
        assert "example" not in content["text/plain"]
        assert "text/csv" not in content

    def test_file_response_becomes_binary(self):
        op = _one_op(
            {
                "produces": ["application/octet-stream"],
                "responses": {"200": {"description": "ok", "schema": {"type": "file"}}},
            }
        )
        schema = op["responses"]["200"]["content"]["application/octet-stream"]["schema"]
        assert schema == {"type": "string", "format": "binary"}
        assert UNSUPPORTED not in op  # a download is a response-shape question for 13.3


# --------------------------------------------------------------------------- schemas


class TestSchemas:
    def test_refs_are_rewritten_everywhere_a_schema_can_be(self):
        ref = {"$ref": "#/definitions/X"}
        new = {"$ref": "#/components/schemas/X"}
        doc = _swagger(
            definitions={
                "X": {"type": "string"},
                "Y": {
                    "type": "object",
                    "properties": {"a": ref},
                    "additionalProperties": ref,
                    "items": ref,
                    "allOf": [ref],
                },
            }
        )
        y = swagger2_to_openapi3(doc)["components"]["schemas"]["Y"]
        assert y["properties"]["a"] == new
        assert y["additionalProperties"] == new
        assert y["items"] == new
        assert y["allOf"] == [new]

    def test_x_nullable_false_is_dropped(self):
        doc = _swagger(definitions={"X": {"type": "string", "x-nullable": False}})
        assert swagger2_to_openapi3(doc)["components"]["schemas"]["X"] == {"type": "string"}

    def test_other_extensions_are_kept(self):
        doc = _swagger(definitions={"X": {"type": "string", "x-go-name": "Ex"}})
        assert swagger2_to_openapi3(doc)["components"]["schemas"]["X"]["x-go-name"] == "Ex"


# --------------------------------------------------------------------------- security


class TestSecuritySchemes:
    @pytest.mark.parametrize(
        "flow, extra, key, expected",
        [
            (
                "implicit",
                {"authorizationUrl": "https://a"},
                "implicit",
                {"authorizationUrl": "https://a"},
            ),
            ("password", {"tokenUrl": "https://t"}, "password", {"tokenUrl": "https://t"}),
            (
                "application",
                {"tokenUrl": "https://t"},
                "clientCredentials",
                {"tokenUrl": "https://t"},
            ),
            (
                "accessCode",
                {"authorizationUrl": "https://a", "tokenUrl": "https://t"},
                "authorizationCode",
                {"authorizationUrl": "https://a", "tokenUrl": "https://t"},
            ),
        ],
    )
    def test_oauth2_flows(self, flow, extra, key, expected):
        scheme = {"type": "oauth2", "flow": flow, "scopes": {"s": "S"}, **extra}
        doc = _swagger(securityDefinitions={"o": scheme})
        converted = swagger2_to_openapi3(doc)["components"]["securitySchemes"]["o"]
        assert converted == {"type": "oauth2", "flows": {key: {**expected, "scopes": {"s": "S"}}}}

    def test_description_is_kept(self):
        doc = _swagger(securityDefinitions={"b": {"type": "basic", "description": "Staff only"}})
        converted = swagger2_to_openapi3(doc)["components"]["securitySchemes"]["b"]
        assert converted == {"type": "http", "scheme": "basic", "description": "Staff only"}


# --------------------------------------------------------------------------- path items


class TestPathItems:
    def test_local_path_item_ref_is_inlined_and_converted(self):
        shared = {
            "get": {
                "parameters": [{"name": "q", "in": "query", "type": "string"}],
                "responses": {"200": {"description": "ok"}},
            },
            "x-owner": "team",
        }
        doc = _swagger(**{"x-shared": shared}, paths={"/a": {"$ref": "#/x-shared"}})
        converted = swagger2_to_openapi3(doc)
        assert converted["paths"]["/a"] == {
            "get": {
                "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok"}},
            },
            "x-owner": "team",
        }
        assert _reload(converted).dialect == "3.0"


class TestFormDetails:
    def test_field_descriptions_are_kept(self):
        op = _one_op(
            {"parameters": [{"name": "f", "in": "formData", "type": "string", "description": "F"}]}
        )
        schema = op["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]
        assert schema["properties"]["f"] == {"type": "string", "description": "F"}

    def test_form_array_with_tsv_is_unsupported(self):
        op = _one_op(
            {
                "parameters": [
                    {
                        "name": "ids",
                        "in": "formData",
                        "type": "array",
                        "items": {"type": "string"},
                        "collectionFormat": "tsv",
                    }
                ]
            }
        )
        assert any("tsv" in reason for reason in op[UNSUPPORTED])
