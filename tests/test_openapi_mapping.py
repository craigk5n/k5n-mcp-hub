"""Story 13.3 -- OpenAPI operations to MCP tools (pure; ADR 0011 §3, §5, §6, §7).

`preview` describes every operation for the admin UI and never raises. `build_tools`
produces the selected tools for tools/list and tools/call, and raises `MappingError`
when the selection cannot be served: a selected operation is unsupported, or two
selected tools would share a name.
"""

from __future__ import annotations

import copy
import json
import pathlib
from typing import Any

import pytest

from mcp_hub.mcp.schema_refs import resolve_tool_schema_refs
from mcp_hub.openapi.mapping import build_tools, preview
from mcp_hub.openapi.model import MappingError, OperationKey, Override

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "openapi"
LIMIT = 1024


def _doc(paths: dict[str, Any], components: dict[str, Any] | None = None, *, v: str = "3.1"):
    doc: dict[str, Any] = {
        "openapi": "3.1.0" if v == "3.1" else "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": paths,
    }
    if components:
        doc["components"] = components
    return doc


def _op(**fields: Any) -> dict[str, Any]:
    return {"responses": {"200": {"description": "ok"}}, **fields}


def _preview(doc: dict[str, Any], *, v: str = "3.1", overrides=None, limit: int = LIMIT):
    return {
        mapped.key: mapped
        for mapped in preview(doc, v, overrides=overrides or {}, description_limit=limit)
    }


def _one(
    operation: dict[str, Any],
    *,
    method: str = "post",
    path: str = "/things",
    components: dict[str, Any] | None = None,
    v: str = "3.1",
    path_item: dict[str, Any] | None = None,
):
    item = {**(path_item or {}), method: operation}
    mapped = _preview(_doc({path: item}, components, v=v), v=v)
    return mapped[OperationKey(method.upper(), path)]


def _tools(doc: dict[str, Any], selection, *, v: str = "3.1", overrides=None):
    keys = frozenset(OperationKey(m, p) for m, p in selection)
    return build_tools(doc, v, selection=keys, overrides=overrides or {}, description_limit=LIMIT)


def _schema(mapped) -> dict[str, Any]:
    assert mapped.tool is not None, mapped.unsupported
    return mapped.tool.input_schema


def _param(name: str, location: str = "query", **fields: Any) -> dict[str, Any]:
    return {"name": name, "in": location, "schema": {"type": "string"}, **fields}


# --------------------------------------------------------------------------- keys


class TestOperationKey:
    def test_method_is_upper_cased(self):
        assert OperationKey("get", "/a") == OperationKey("GET", "/a")
        assert str(OperationKey("get", "/a")) == "GET /a"

    def test_every_operation_is_previewed(self):
        doc = _doc(
            {
                "/a": {"get": _op(), "post": _op(), "parameters": [], "x-ext": 1},
                "/b/{id}": {"delete": _op(parameters=[_param("id", "path", required=True)])},
                "x-paths-ext": {"get": _op()},
            }
        )
        assert set(_preview(doc)) == {
            OperationKey("GET", "/a"),
            OperationKey("POST", "/a"),
            OperationKey("DELETE", "/b/{id}"),
        }


# --------------------------------------------------------------------------- naming


class TestNaming:
    @pytest.mark.parametrize(
        "operation_id, name",
        [
            ("listPets", "listPets"),
            ("pets.list", "pets.list"),
            ("pets-list_v2", "pets-list_v2"),
            ("get pets/{id}", "get_pets_id"),
            ("Ünïcödé", "n_c_d"),
            ("a" * 200, "a" * 128),
        ],
    )
    def test_operation_id_is_normalized(self, operation_id, name):
        assert _one(_op(operationId=operation_id)).tool.name == name

    @pytest.mark.parametrize(
        "method, path, name",
        [
            ("get", "/pets", "get_pets"),
            ("get", "/pets/{petId}", "get_pets_petId"),
            ("delete", "/v1/users/{id}/keys", "delete_v1_users_id_keys"),
            ("get", "/", "get"),
        ],
    )
    def test_falls_back_to_method_and_path(self, method, path, name):
        params = [_param(p, "path", required=True) for p in ("petId", "id") if f"{{{p}}}" in path]
        assert _one(_op(parameters=params), method=method, path=path).tool.name == name

    def test_operation_id_that_normalizes_to_nothing_falls_back(self):
        assert _one(_op(operationId="!!!")).tool.name == "post_things"

    def test_override_wins(self):
        doc = _doc({"/a": {"get": _op(operationId="listA")}})
        key = OperationKey("GET", "/a")
        mapped = _preview(doc, overrides={key: Override(name="list_everything")})[key]
        assert mapped.tool.name == "list_everything"

    @pytest.mark.parametrize("bad", ["", "has space", "x" * 129, "slash/name"])
    def test_invalid_override_name_is_a_problem_not_a_rename(self, bad):
        doc = _doc({"/a": {"get": _op(operationId="listA")}})
        key = OperationKey("GET", "/a")
        mapped = _preview(doc, overrides={key: Override(name=bad)})[key]
        assert mapped.tool.name == "listA"
        assert any("override" in p for p in mapped.problems)
        with pytest.raises(MappingError, match="override"):
            _tools(doc, [("GET", "/a")], overrides={key: Override(name=bad)})

    def test_collision_is_shown_on_both_in_the_preview(self):
        doc = _doc(
            {"/a": {"get": _op(operationId="same")}, "/b": {"post": _op(operationId="same")}}
        )
        mapped = _preview(doc)
        assert any("POST /b" in p for p in mapped[OperationKey("GET", "/a")].problems)
        assert any("GET /a" in p for p in mapped[OperationKey("POST", "/b")].problems)

    def test_collision_after_normalization_counts(self):
        doc = _doc(
            {
                "/a": {"get": _op(operationId="list pets")},
                "/b": {"get": _op(operationId="list/pets")},
            }
        )
        assert _preview(doc)[OperationKey("GET", "/a")].problems

    def test_collision_only_blocks_when_both_are_selected(self):
        doc = _doc(
            {"/a": {"get": _op(operationId="same")}, "/b": {"post": _op(operationId="same")}}
        )
        assert [t.definition.name for t in _tools(doc, [("GET", "/a")])] == ["same"]
        with pytest.raises(MappingError) as excinfo:
            _tools(doc, [("GET", "/a"), ("POST", "/b")])
        assert "GET /a" in str(excinfo.value) and "POST /b" in str(excinfo.value)

    def test_override_resolves_a_collision(self):
        doc = _doc(
            {"/a": {"get": _op(operationId="same")}, "/b": {"post": _op(operationId="same")}}
        )
        overrides = {OperationKey("POST", "/b"): Override(name="same_b")}
        tools = _tools(doc, [("GET", "/a"), ("POST", "/b")], overrides=overrides)
        assert sorted(t.definition.name for t in tools) == ["same", "same_b"]

    def test_names_are_case_sensitive(self):
        doc = _doc({"/a": {"get": _op(operationId="List")}, "/b": {"get": _op(operationId="list")}})
        assert len(_tools(doc, [("GET", "/a"), ("GET", "/b")])) == 2


# --------------------------------------------------------------------------- descriptions


class TestDescriptions:
    @pytest.mark.parametrize(
        "fields, description",
        [
            ({"summary": "List pets", "description": "Paged."}, "List pets\n\nPaged."),
            ({"summary": "List pets"}, "List pets"),
            ({"description": "  Paged.  "}, "Paged."),
            ({}, "POST /things"),
        ],
    )
    def test_summary_and_description(self, fields, description):
        assert _one(_op(**fields)).tool.description == description

    def test_capped(self):
        tool = _preview(_doc({"/a": {"get": _op(description="x" * 5000)}}), limit=100)[
            OperationKey("GET", "/a")
        ].tool
        assert len(tool.description) == 100
        assert tool.description.endswith("…")

    def test_override_is_used_and_capped(self):
        doc = _doc({"/a": {"get": _op(summary="from spec")}})
        key = OperationKey("GET", "/a")
        tool = _preview(doc, overrides={key: Override(description="y" * 500)}, limit=50)[key].tool
        assert tool.description == "y" * 49 + "…"


# --------------------------------------------------------------------------- parameters


class TestParameters:
    def test_parameters_become_top_level_properties(self):
        op = _op(
            parameters=[
                _param("id", "path", required=True, description="The id"),
                _param("limit", "query", schema={"type": "integer", "maximum": 100}),
                _param("X-Trace", "header"),
            ]
        )
        schema = _schema(_one(op, path="/things/{id}", method="get"))
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert schema["properties"] == {
            "id": {"type": "string", "description": "The id"},
            "limit": {"type": "integer", "maximum": 100},
            "X-Trace": {"type": "string"},
        }
        assert schema["required"] == ["id"]

    def test_a_schema_description_is_not_overwritten(self):
        param = _param(
            "q", schema={"type": "string", "description": "from schema"}, description="p"
        )
        assert (
            _schema(_one(_op(parameters=[param])))["properties"]["q"]["description"]
            == "from schema"
        )

    def test_path_level_parameters_merge_with_operation_ones(self):
        item = {"parameters": [_param("q"), _param("keep")]}
        op = _op(parameters=[_param("q", schema={"type": "integer"})])
        schema = _schema(_one(op, method="get", path_item=item))
        assert schema["properties"]["q"] == {"type": "integer"}
        assert "keep" in schema["properties"]

    def test_parameter_refs_resolve(self):
        components = {"parameters": {"Limit": _param("limit", schema={"type": "integer"})}}
        op = _op(parameters=[{"$ref": "#/components/parameters/Limit"}])
        assert _schema(_one(op, components=components))["properties"]["limit"] == {
            "type": "integer"
        }

    def test_plan_records_where_each_argument_goes(self):
        op = _op(
            parameters=[
                _param("id", "path", required=True),
                _param(
                    "tags",
                    "query",
                    explode=False,
                    schema={"type": "array", "items": {"type": "string"}},
                ),
            ]
        )
        plan = _one(op, path="/things/{id}", method="get").plan
        assert plan.method == "GET" and plan.path == "/things/{id}"
        by_arg = {p.argument: p for p in plan.parameters}
        assert (by_arg["id"].name, by_arg["id"].location, by_arg["id"].style) == (
            "id",
            "path",
            "simple",
        )
        assert (by_arg["tags"].style, by_arg["tags"].explode) == ("form", False)

    def test_default_styles(self):
        op = _op(parameters=[_param("q"), _param("h", "header")])
        by_arg = {p.argument: p for p in _one(op).plan.parameters}
        assert (by_arg["q"].style, by_arg["q"].explode) == ("form", True)
        assert (by_arg["h"].style, by_arg["h"].explode) == ("simple", False)

    @pytest.mark.parametrize(
        "name",
        [
            "Authorization",
            "authorization",
            "Proxy-Authorization",
            "Cookie",
            "Host",
            "Content-Length",
            "Content-Type",
            "Transfer-Encoding",
            "Connection",
            "Forwarded",
            "X-Forwarded-For",
        ],
    )
    def test_credential_and_transport_headers_are_dropped(self, name):
        mapped = _one(_op(parameters=[_param(name, "header", required=True)]))
        assert name not in _schema(mapped)["properties"]
        assert name not in [p.name for p in mapped.plan.parameters]
        assert any(name in d for d in mapped.dropped)

    def test_cookie_parameters_are_dropped(self):
        mapped = _one(_op(parameters=[_param("session", "cookie")]))
        assert "session" not in _schema(mapped)["properties"]
        assert any("session" in d for d in mapped.dropped)

    @pytest.mark.parametrize(
        "param, reason",
        [
            (
                {
                    "name": "q",
                    "in": "query",
                    "content": {"application/json": {"schema": {"type": "object"}}},
                },
                "content",
            ),
            (_param("q", style="deepObject", schema={"type": "object"}), "deepObject"),
            (
                _param("q", schema={"type": "object", "properties": {"a": {"type": "string"}}}),
                "object",
            ),
            (
                _param(
                    "q",
                    style="spaceDelimited",
                    schema={"type": "array", "items": {"type": "string"}},
                ),
                "spaceDelimited",
            ),
            (_param("q", schema={"type": "array", "items": {"type": "array"}}), "array of arrays"),
            (_param("h", "header", style="form"), "form"),
        ],
    )
    def test_unsupported_serializations(self, param, reason):
        mapped = _one(_op(parameters=[param]))
        assert mapped.tool is None
        assert any(reason in r for r in mapped.unsupported)

    @pytest.mark.parametrize("style", ["label", "matrix"])
    def test_unsupported_path_styles(self, style):
        op = _op(parameters=[_param("id", "path", required=True, style=style)])
        mapped = _one(op, path="/things/{id}")
        assert any(style in r for r in mapped.unsupported)

    def test_undeclared_path_variable(self):
        mapped = _one(_op(), path="/things/{id}")
        assert any("{id}" in r for r in mapped.unsupported)


# --------------------------------------------------------------------------- bodies


OBJECT_BODY = {
    "type": "object",
    "required": ["name"],
    "properties": {"name": {"type": "string"}, "tag": {"type": "string"}},
}


def _body(schema: Any, media: str = "application/json", required: bool = True) -> dict[str, Any]:
    return {"required": required, "content": {media: {"schema": schema}}}


class TestBodies:
    def test_object_body_is_merged(self):
        mapped = _one(_op(parameters=[_param("dry_run")], requestBody=_body(OBJECT_BODY)))
        schema = _schema(mapped)
        assert set(schema["properties"]) == {"dry_run", "name", "tag"}
        assert schema["required"] == ["name"]
        body = mapped.plan.body
        assert body.mode == "merged" and body.media_type == "application/json"
        assert dict(body.fields) == {"name": "name", "tag": "tag"}

    def test_optional_body_fields_are_not_required(self):
        schema = _schema(_one(_op(requestBody=_body(OBJECT_BODY, required=False))))
        assert "required" not in schema

    def test_colliding_body_property_is_renamed(self):
        body = {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}
        op = _op(parameters=[_param("id", "path", required=True)], requestBody=_body(body))
        mapped = _one(op, path="/things/{id}")
        schema = _schema(mapped)
        assert schema["properties"]["id"] == {"type": "string"}
        assert schema["properties"]["body_id"] == {"type": "integer"}
        assert schema["required"] == ["id", "body_id"]
        assert dict(mapped.plan.body.fields) == {"body_id": "id"}

    def test_double_collision_is_unsupported(self):
        body = {"type": "object", "properties": {"id": {"type": "integer"}}}
        params = [_param("id", "path", required=True), _param("body_id")]
        mapped = _one(_op(parameters=params, requestBody=_body(body)), path="/things/{id}")
        assert mapped.tool is None
        assert any("body_id" in r for r in mapped.unsupported)

    def test_body_property_colliding_with_another_after_rename(self):
        body = {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "body_id": {"type": "string"}},
        }
        mapped = _one(
            _op(parameters=[_param("id", "path", required=True)], requestBody=_body(body)),
            path="/things/{id}",
        )
        assert any("body_id" in r for r in mapped.unsupported)

    @pytest.mark.parametrize(
        "schema",
        [
            {"type": "array", "items": {"type": "string"}},
            {"type": "string"},
            {"type": "integer"},
            {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            {"anyOf": [OBJECT_BODY, {"type": "string"}]},
            {"type": "object", "additionalProperties": True},
            {"type": "object", "additionalProperties": {"type": "integer"}},
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "additionalProperties": {"type": "string"},
            },
            {"type": "object", "patternProperties": {"^x": {"type": "string"}}},
            {"allOf": [OBJECT_BODY, {"oneOf": [{"type": "object"}]}]},
        ],
        ids=[
            "array",
            "string",
            "integer",
            "oneOf",
            "anyOf",
            "additional-true",
            "additional-schema",
            "props-and-additional",
            "pattern-properties",
            "allOf-with-oneOf",
        ],
    )
    def test_non_mergeable_bodies_go_under_body(self, schema):
        mapped = _one(_op(requestBody=_body(schema)))
        input_schema = _schema(mapped)
        assert list(input_schema["properties"]) == ["body"]
        assert input_schema["required"] == ["body"]
        assert mapped.plan.body.mode == "whole"
        assert dict(mapped.plan.body.fields) == {"body": ""}

    def test_whole_body_named_body_collides_with_a_parameter_named_body(self):
        mapped = _one(_op(parameters=[_param("body")], requestBody=_body({"type": "array"})))
        assert set(_schema(mapped)["properties"]) == {"body", "body_body"}

    def test_additional_properties_false_still_merges(self):
        schema = {**OBJECT_BODY, "additionalProperties": False}
        assert _one(_op(requestBody=_body(schema))).plan.body.mode == "merged"

    def test_ref_to_an_object_merges(self):
        components = {
            "schemas": {"NewPet": OBJECT_BODY, "Alias": {"$ref": "#/components/schemas/NewPet"}}
        }
        op = _op(requestBody=_body({"$ref": "#/components/schemas/Alias"}))
        assert set(_schema(_one(op, components=components))["properties"]) == {"name", "tag"}

    def test_request_body_ref_resolves(self):
        components = {"requestBodies": {"Pet": _body(OBJECT_BODY)}}
        op = _op(requestBody={"$ref": "#/components/requestBodies/Pet"})
        assert set(_schema(_one(op, components=components))["properties"]) == {"name", "tag"}

    def test_all_of_objects_merge(self):
        components = {"schemas": {"NewPet": OBJECT_BODY}}
        schema = {
            "allOf": [
                {"$ref": "#/components/schemas/NewPet"},
                {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}},
            ]
        }
        mapped = _one(_op(requestBody=_body(schema)), components=components)
        input_schema = _schema(mapped)
        assert set(input_schema["properties"]) == {"name", "tag", "id"}
        assert input_schema["required"] == ["name", "id"]

    @pytest.mark.parametrize(
        "media",
        [
            "application/json",
            "application/vnd.api+json",
            "application/merge-patch+json",
            "application/json; charset=utf-8",
        ],
    )
    def test_json_media_types(self, media):
        assert _one(_op(requestBody=_body(OBJECT_BODY, media))).plan.body.media_type == media

    def test_form_body_merges(self):
        mapped = _one(_op(requestBody=_body(OBJECT_BODY, "application/x-www-form-urlencoded")))
        assert mapped.plan.body.media_type == "application/x-www-form-urlencoded"
        assert mapped.plan.body.mode == "merged"

    def test_form_body_must_be_an_object(self):
        mapped = _one(
            _op(requestBody=_body({"type": "string"}, "application/x-www-form-urlencoded"))
        )
        assert any("form" in r for r in mapped.unsupported)

    def test_json_is_preferred_over_form(self):
        body = {
            "content": {
                "application/x-www-form-urlencoded": {"schema": OBJECT_BODY},
                "application/json": {"schema": OBJECT_BODY},
            }
        }
        assert _one(_op(requestBody=body)).plan.body.media_type == "application/json"

    @pytest.mark.parametrize(
        "media",
        ["multipart/form-data", "application/xml", "application/octet-stream", "text/plain"],
    )
    def test_other_body_types_are_unsupported(self, media):
        mapped = _one(_op(requestBody=_body(OBJECT_BODY, media)))
        assert mapped.tool is None
        assert any(media in r for r in mapped.unsupported)

    def test_body_without_a_schema_is_unsupported(self):
        mapped = _one(_op(requestBody={"content": {"application/json": {}}}))
        assert any("schema" in r for r in mapped.unsupported)


# --------------------------------------------------------------------------- schema conversion


class TestSchemaConversion:
    def _converted(self, schema: dict[str, Any], v: str = "3.0") -> dict[str, Any]:
        op = _op(parameters=[{"name": "p", "in": "query", "schema": schema}])
        return _schema(_one(op, v=v))["properties"]["p"]

    @pytest.mark.parametrize(
        "schema, expected",
        [
            ({"type": "string", "nullable": True}, {"type": ["string", "null"]}),
            ({"type": "string", "nullable": False}, {"type": "string"}),
            (
                {"type": "string", "enum": ["a"], "nullable": True},
                {"type": ["string", "null"], "enum": ["a", None]},
            ),
            (
                {"allOf": [{"type": "string"}], "nullable": True},
                {"anyOf": [{"allOf": [{"type": "string"}]}, {"type": "null"}]},
            ),
            (
                {"type": "integer", "minimum": 1, "exclusiveMinimum": True},
                {"type": "integer", "exclusiveMinimum": 1},
            ),
            (
                {"type": "integer", "minimum": 1, "exclusiveMinimum": False},
                {"type": "integer", "minimum": 1},
            ),
            (
                {"type": "integer", "maximum": 9, "exclusiveMaximum": True},
                {"type": "integer", "exclusiveMaximum": 9},
            ),
            ({"type": "string", "example": "x"}, {"type": "string", "examples": ["x"]}),
            (
                {
                    "type": "string",
                    "discriminator": {"propertyName": "k"},
                    "xml": {},
                    "externalDocs": {"url": "u"},
                    "x-ext": 1,
                },
                {"type": "string"},
            ),
            (
                {"type": "array", "items": {"type": "string", "nullable": True}},
                {"type": "array", "items": {"type": ["string", "null"]}},
            ),
        ],
    )
    def test_30_to_2020_12(self, schema, expected):
        assert self._converted(schema) == expected

    def test_nested_positions_are_converted(self):
        # An object with additionalProperties goes whole, so the converted schema
        # sits under the `body` argument.
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string", "nullable": True}},
            "additionalProperties": {"type": "integer", "example": 1},
            "allOf": [{"type": "object", "example": {}}],
            "not": {"type": "string", "nullable": True},
        }
        mapped = _one(_op(requestBody=_body(schema)), v="3.0")
        converted = _schema(mapped)["properties"]["body"]
        assert converted["properties"]["a"] == {"type": ["string", "null"]}
        assert converted["additionalProperties"] == {"type": "integer", "examples": [1]}
        assert converted["allOf"] == [{"type": "object", "examples": [{}]}]
        assert converted["not"] == {"type": ["string", "null"]}

    def test_30_ref_siblings_are_ignored_as_30_specifies(self):
        components = {"schemas": {"S": {"type": "string"}}}
        op = _op(
            parameters=[
                {
                    "name": "p",
                    "in": "query",
                    "schema": {"$ref": "#/components/schemas/S", "description": "ignored"},
                }
            ]
        )
        schema = _schema(_one(op, components=components, v="3.0"))
        assert schema["properties"]["p"] == {"$ref": "#/$defs/S"}

    def test_31_ref_siblings_are_kept(self):
        components = {"schemas": {"S": {"type": "string"}}}
        op = _op(
            parameters=[
                {
                    "name": "p",
                    "in": "query",
                    "schema": {"$ref": "#/components/schemas/S", "description": "kept"},
                }
            ]
        )
        schema = _schema(_one(op, components=components, v="3.1"))
        assert schema["properties"]["p"] == {"$ref": "#/$defs/S", "description": "kept"}

    def test_31_schemas_are_left_alone_apart_from_openapi_keywords(self):
        schema = {
            "type": ["string", "null"],
            "examples": ["x"],
            "exclusiveMinimum": 3,
            "discriminator": {"propertyName": "k"},
        }
        assert self._converted(schema, v="3.1") == {
            "type": ["string", "null"],
            "examples": ["x"],
            "exclusiveMinimum": 3,
        }


class TestBundling:
    def test_refs_are_bundled_into_defs_transitively(self):
        components = {
            "schemas": {
                "Pet": {
                    "type": "object",
                    "properties": {"owner": {"$ref": "#/components/schemas/Owner"}},
                },
                "Owner": {"type": "object", "properties": {"name": {"type": "string"}}},
                "Unused": {"type": "string"},
            }
        }
        op = _op(
            requestBody=_body({"type": "array", "items": {"$ref": "#/components/schemas/Pet"}})
        )
        schema = _schema(_one(op, components=components))
        assert schema["properties"]["body"]["items"] == {"$ref": "#/$defs/Pet"}
        assert set(schema["$defs"]) == {"Pet", "Owner"}
        assert schema["$defs"]["Pet"]["properties"]["owner"] == {"$ref": "#/$defs/Owner"}

    def test_recursive_schema_terminates(self):
        components = {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "children": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Node"},
                        }
                    },
                }
            }
        }
        op = _op(requestBody=_body({"$ref": "#/components/schemas/Node"}))
        schema = _schema(_one(op, components=components))
        assert schema["properties"]["children"]["items"] == {"$ref": "#/$defs/Node"}
        assert schema["$defs"]["Node"]["properties"]["children"]["items"] == {
            "$ref": "#/$defs/Node"
        }

    def test_non_component_targets_get_a_safe_unique_name(self):
        components = {
            "schemas": {"Thing": {"type": "string"}},
            "parameters": {"P": {"name": "p", "in": "query", "schema": {"type": "integer"}}},
        }
        op = _op(
            parameters=[
                {"name": "a", "in": "query", "schema": {"$ref": "#/components/schemas/Thing"}},
                {
                    "name": "b",
                    "in": "query",
                    "schema": {"$ref": "#/components/parameters/P/schema"},
                },
            ]
        )
        schema = _schema(_one(op, components=components))
        names = set(schema["$defs"])
        assert "Thing" in names and len(names) == 2
        assert all(n.replace("_", "").replace("-", "").replace(".", "").isalnum() for n in names)

    def test_schema_refs_display_resolver_works_on_mapped_tools(self):
        # The existing resolver used for tool display (schema_refs.py) reads
        # #/$defs refs, which is exactly what bundling produces.
        components = {
            "schemas": {
                "Color": {"type": "string", "enum": ["red", "blue"], "description": "A color"}
            }
        }
        op = _op(
            parameters=[
                {"name": "c", "in": "query", "schema": {"$ref": "#/components/schemas/Color"}}
            ]
        )
        tool = _one(op, components=components).tool.to_mcp()
        resolve_tool_schema_refs([tool])
        assert tool["inputSchema"]["properties"]["c"]["enum"] == ["red", "blue"]


# --------------------------------------------------------------------------- output


def _responses(code: str, schema: Any, media: str = "application/json") -> dict[str, Any]:
    return {code: {"description": "ok", "content": {media: {"schema": schema}}}}


class TestOutputSchema:
    def test_object_response(self):
        tool = _one(_op(responses=_responses("200", OBJECT_BODY)), method="get").tool
        assert tool.output_schema == OBJECT_BODY

    def test_non_object_is_wrapped_as_result(self):
        schema = {"type": "array", "items": {"type": "string"}}
        tool = _one(_op(responses=_responses("200", schema)), method="get").tool
        assert tool.output_schema == {
            "type": "object",
            "properties": {"result": schema},
            "required": ["result"],
        }

    def test_ref_to_object_has_an_explicit_object_root(self):
        components = {"schemas": {"Pet": OBJECT_BODY}}
        tool = _one(
            _op(responses=_responses("200", {"$ref": "#/components/schemas/Pet"})),
            method="get",
            components=components,
        ).tool
        assert tool.output_schema["type"] == "object"
        assert tool.output_schema["properties"] == OBJECT_BODY["properties"]

    def test_nullable_object_root_is_wrapped(self):
        schema = {"type": "object", "nullable": True, "properties": {}}
        tool = _one(_op(responses=_responses("200", schema)), method="get", v="3.0").tool
        assert set(tool.output_schema["properties"]) == {"result"}

    @pytest.mark.parametrize(
        "responses, has_output",
        [
            ({**_responses("201", OBJECT_BODY), "400": {"description": "bad"}}, True),
            (_responses("2XX", OBJECT_BODY), True),
            (_responses("200", OBJECT_BODY, "text/plain"), False),
            ({"204": {"description": "none"}}, False),
            (_responses("default", OBJECT_BODY), False),
            ({**_responses("404", OBJECT_BODY), "200": {"description": "no body"}}, False),
        ],
    )
    def test_which_response_is_used(self, responses, has_output):
        tool = _one(_op(responses=responses), method="get").tool
        assert (tool.output_schema is not None) == has_output

    def test_lowest_2xx_wins(self):
        responses = {**_responses("202", {"type": "string"}), **_responses("200", OBJECT_BODY)}
        assert _one(_op(responses=responses), method="get").tool.output_schema == OBJECT_BODY

    def test_response_ref_resolves(self):
        components = {
            "responses": {
                "Ok": {
                    "description": "ok",
                    "content": {"application/json": {"schema": OBJECT_BODY}},
                }
            }
        }
        op = _op(responses={"200": {"$ref": "#/components/responses/Ok"}})
        assert _one(op, method="get", components=components).tool.output_schema == OBJECT_BODY

    def test_output_bundle_is_independent_of_input(self):
        components = {"schemas": {"In": {"type": "string"}, "Out": OBJECT_BODY}}
        op = _op(
            parameters=[
                {"name": "q", "in": "query", "schema": {"$ref": "#/components/schemas/In"}}
            ],
            responses=_responses(
                "200", {"type": "array", "items": {"$ref": "#/components/schemas/Out"}}
            ),
        )
        tool = _one(op, method="get", components=components).tool
        assert set(tool.input_schema["$defs"]) == {"In"}
        assert set(tool.output_schema["$defs"]) == {"Out"}


# --------------------------------------------------------------------------- annotations


class TestAnnotations:
    @pytest.mark.parametrize(
        "method, annotations",
        [
            ("get", {"readOnlyHint": True}),
            ("head", {"readOnlyHint": True}),
            ("delete", {"destructiveHint": True}),
            ("put", {"idempotentHint": True}),
            ("post", {}),
            ("patch", {}),
            ("options", {}),
        ],
    )
    def test_by_method(self, method, annotations):
        assert _one(_op(), method=method).tool.annotations == annotations

    def test_mcp_shape(self):
        tool = _one(_op(operationId="listThings", summary="List"), method="get").tool.to_mcp()
        assert tool == {
            "name": "listThings",
            "description": "List",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        }


# --------------------------------------------------------------------------- unsupported & selection


class TestUnsupported:
    def test_trace_is_unsupported(self):
        # TRACE echoes the request -- including the Authorization header the hub
        # adds -- back in the response, which would hand the model the credential.
        mapped = _one(_op(), method="trace")
        assert mapped.tool is None
        assert any("TRACE" in r for r in mapped.unsupported)

    def test_swagger_markers_are_surfaced(self):
        doc = json.loads((FIXTURES / "swagger2-kitchen-sink.json").read_text())
        mapped = _preview(doc, v="2.0")
        upload = mapped[OperationKey("POST", "/pets/{petId}/photo")]
        assert upload.tool is None
        assert any("file upload" in r for r in upload.unsupported)
        assert mapped[OperationKey("GET", "/pets")].tool.name == "listPets"

    def test_selecting_an_unsupported_operation_is_an_error(self):
        doc = _doc({"/a": {"trace": _op()}, "/b": {"get": _op()}})
        with pytest.raises(MappingError, match="TRACE /a"):
            _tools(doc, [("TRACE", "/a"), ("GET", "/b")])

    def test_problems_are_reported_together(self):
        doc = _doc(
            {
                "/a": {"trace": _op()},
                "/b": {"get": _op(operationId="x")},
                "/c": {"get": _op(operationId="x")},
            }
        )
        with pytest.raises(MappingError) as excinfo:
            _tools(doc, [("TRACE", "/a"), ("GET", "/b"), ("GET", "/c")])
        assert len(excinfo.value.problems) == 3


class TestSelection:
    def test_only_selected_operations_become_tools(self):
        doc = _doc({"/a": {"get": _op(operationId="a"), "post": _op(operationId="b")}})
        tools = _tools(doc, [("POST", "/a")])
        assert [t.definition.name for t in tools] == ["b"]
        assert tools[0].plan.method == "POST"

    def test_selection_of_an_operation_no_longer_in_the_spec_is_ignored(self):
        # Orphans are Story 13.7's to report; tools/list must keep working.
        doc = _doc({"/a": {"get": _op(operationId="a")}})
        assert [t.definition.name for t in _tools(doc, [("GET", "/a"), ("GET", "/gone")])] == ["a"]

    def test_preview_marks_selected(self):
        doc = _doc({"/a": {"get": _op(), "post": _op()}})
        selection = frozenset({OperationKey("GET", "/a")})
        mapped = {
            m.key: m
            for m in preview(doc, "3.1", overrides={}, description_limit=LIMIT, selection=selection)
        }
        assert mapped[OperationKey("GET", "/a")].selected
        assert not mapped[OperationKey("POST", "/a")].selected


class TestPurity:
    def test_document_is_not_modified_and_nothing_is_shared(self):
        doc = json.loads((FIXTURES / "petstore-3.1.json").read_text())
        before = copy.deepcopy(doc)
        tools = _tools(doc, [("GET", "/pets"), ("POST", "/pets"), ("GET", "/pets/{petId}")])
        assert doc == before
        for tool in tools:
            tool.definition.input_schema.setdefault("properties", {})["mutated"] = {}
            if tool.definition.output_schema:
                tool.definition.output_schema["mutated"] = True
        assert doc == before

    def test_petstore_end_to_end(self):
        doc = json.loads((FIXTURES / "petstore-3.1.json").read_text())
        tools = {
            t.definition.name: t
            for t in _tools(doc, [("GET", "/pets"), ("POST", "/pets"), ("GET", "/pets/{petId}")])
        }
        assert set(tools) == {"listPets", "createPet", "showPetById"}
        create = tools["createPet"].definition
        assert set(create.input_schema["properties"]) == {"name", "tag"}
        assert create.input_schema["required"] == ["name"]
        assert tools["listPets"].definition.output_schema["properties"]["result"]["type"] == "array"


class TestSchemasAreValidJsonSchema:
    """Every tool schema must be a valid 2020-12 schema on its own: MCP clients and
    Story 13.5's argument validation read it without the spec."""

    @pytest.mark.parametrize(
        "name, dialect",
        [
            ("petstore-3.1.json", "3.1"),
            ("petstore-3.0.yaml", "3.0"),
            ("swagger2-kitchen-sink.json", "2.0"),
        ],
    )
    def test_fixture_tools(self, name, dialect):
        from jsonschema import Draft202012Validator

        from mcp_hub.openapi.loader import load_spec_unsandboxed

        spec = load_spec_unsandboxed((FIXTURES / name).read_bytes(), max_bytes=5 * 1024 * 1024)
        assert spec.dialect == dialect
        mapped = preview(spec.document, spec.dialect, overrides={}, description_limit=LIMIT)
        tools = [m.tool for m in mapped if m.tool is not None]
        assert tools
        for tool in tools:
            Draft202012Validator.check_schema(tool.input_schema)
            if tool.output_schema is not None:
                Draft202012Validator.check_schema(tool.output_schema)
                assert tool.output_schema["type"] == "object"


class TestEdgeCases:
    def test_two_parameters_with_one_name_is_unsupported(self):
        # Parameters keep their names, so `q` in the query and `q` in a header would
        # be one argument for two places.
        mapped = _one(_op(parameters=[_param("q"), _param("q", "header")]))
        assert any("'q'" in r for r in mapped.unsupported)

    def test_path_item_ref(self):
        doc = _doc({"/a": {"$ref": "#/x-items/shared"}})
        doc["x-items"] = {"shared": {"get": _op(operationId="shared")}}
        assert _preview(doc)[OperationKey("GET", "/a")].tool.name == "shared"

    def test_defs_names_that_sanitize_alike_stay_distinct(self):
        doc = _doc(
            {
                "/a": {
                    "post": _op(
                        requestBody=_body(
                            {
                                "type": "array",
                                "items": {
                                    "anyOf": [
                                        {"$ref": "#/x-defs/a b"},
                                        {"$ref": "#/x-defs/a_b"},
                                    ]
                                },
                            }
                        )
                    )
                }
            }
        )
        doc["x-defs"] = {"a b": {"type": "string"}, "a_b": {"type": "integer"}}
        schema = _schema(_preview(doc)[OperationKey("POST", "/a")])
        refs = [r["$ref"] for r in schema["properties"]["body"]["items"]["anyOf"]]
        assert len(set(refs)) == 2
        assert {schema["$defs"][r.split("/")[-1]]["type"] for r in refs} == {"string", "integer"}


class TestStory135Additions:
    """What Story 13.5's executor needs from mapping."""

    @pytest.mark.parametrize(
        "responses, kind",
        [
            (_responses("200", OBJECT_BODY), "object"),
            (_responses("200", {"type": "array"}), "wrapped"),
            ({"200": {"description": "ok"}}, "none"),
        ],
    )
    def test_plan_records_the_output_kind(self, responses, kind):
        assert _one(_op(responses=responses), method="get").plan.output == kind

    @pytest.mark.parametrize("path", ["/a/../admin", "/a/./b", "/.."])
    def test_dot_segments_in_the_path_template_are_unsupported(self, path):
        mapped = _one(_op(), path=path, method="get")
        assert any("'..'" in r or "'.'" in r for r in mapped.unsupported)

    def test_patterns_the_hub_cannot_check_are_noted(self):
        op = _op(parameters=[_param("q", schema={"type": "string", "pattern": r"^(a)\1$"})])
        mapped = _one(op)
        assert mapped.tool is not None
        assert any(r"(a)\1" in note for note in mapped.notes)

    def test_checkable_patterns_are_not_noted(self):
        op = _op(parameters=[_param("q", schema={"type": "string", "pattern": "^[a-z]+$"})])
        assert _one(op).notes == ()


class TestStory135ReviewFixes:
    @pytest.mark.parametrize("path", ["@evil.example.com/x", "x/y"])
    def test_a_path_must_start_with_a_slash(self, path):
        mapped = _one(_op(), path=path, method="get")
        assert any("start with '/'" in r for r in mapped.unsupported)

    @pytest.mark.parametrize("path", ["/%2e%2e/admin", "/a%2Fb", "/a\\..\\b", "/x%20y"])
    def test_percent_encoding_or_backslash_in_a_template_is_unsupported(self, path):
        mapped = _one(_op(), path=path, method="get")
        assert mapped.tool is None

    @pytest.mark.parametrize(
        "name",
        [
            "X-Original-URL",
            "X-Rewrite-URL",
            "X-HTTP-Method-Override",
            "X-HTTP-Method",
            "X-Method-Override",
            "X-Real-IP",
            "Via",
            "Origin",
        ],
    )
    def test_routing_and_override_headers_are_dropped(self, name):
        mapped = _one(_op(parameters=[_param(name, "header")]))
        assert name not in _schema(mapped)["properties"]
