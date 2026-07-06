import copy
from typing import Any

import pytest

from mcp_hub.mcp.schema_refs import resolve_tool_schema_refs


class TestResolveToolSchemaRefs:
    def test_resolves_dollar_defs_ref(self) -> None:
        tools = [
            {
                "inputSchema": {
                    "$defs": {"Foo": {"type": "string", "title": "Foo"}},
                    "properties": {"x": {"$ref": "#/$defs/Foo"}},
                },
            },
        ]
        resolve_tool_schema_refs(tools)
        prop_x = tools[0]["inputSchema"]["properties"]["x"]
        assert prop_x["type"] == "string"
        assert prop_x["title"] == "Foo"

    def test_resolves_definitions_ref(self) -> None:
        tools = [
            {
                "inputSchema": {
                    "definitions": {"Bar": {"type": "number", "title": "Bar"}},
                    "properties": {"y": {"$ref": "#/definitions/Bar"}},
                },
            },
        ]
        resolve_tool_schema_refs(tools)
        prop_y = tools[0]["inputSchema"]["properties"]["y"]
        assert prop_y["type"] == "number"
        assert prop_y["title"] == "Bar"

    def test_existing_property_value_not_overwritten(self) -> None:
        tools = [
            {
                "inputSchema": {
                    "$defs": {"Foo": {"type": "string", "title": "Foo", "description": "def desc"}},
                    "properties": {"x": {"$ref": "#/$defs/Foo", "title": "my title"}},
                },
            },
        ]
        resolve_tool_schema_refs(tools)
        prop_x = tools[0]["inputSchema"]["properties"]["x"]
        assert prop_x["title"] == "my title"
        assert prop_x["description"] == "def desc"

    def test_skips_tool_without_defs_or_definitions(self) -> None:
        tools = [
            {
                "inputSchema": {
                    "properties": {"x": {"$ref": "#/$defs/Foo"}},
                },
            },
        ]
        original = copy.deepcopy(tools)
        resolve_tool_schema_refs(tools)
        assert tools == original

    def test_skips_tool_without_properties(self) -> None:
        tools = [
            {
                "inputSchema": {
                    "$defs": {"Foo": {"type": "string"}},
                },
            },
        ]
        original = copy.deepcopy(tools)
        resolve_tool_schema_refs(tools)
        assert tools == original

    def test_skips_property_without_ref(self) -> None:
        tools = [
            {
                "inputSchema": {
                    "$defs": {"Foo": {"type": "string"}},
                    "properties": {"x": {"type": "integer"}},
                },
            },
        ]
        resolve_tool_schema_refs(tools)
        prop_x = tools[0]["inputSchema"]["properties"]["x"]
        assert prop_x["type"] == "integer"

    def test_cyclic_refs_terminate_within_depth_8(self) -> None:
        tools = [
            {
                "inputSchema": {
                    "$defs": {
                        "A": {"$ref": "#/$defs/B", "type": "string"},
                        "B": {"$ref": "#/$defs/A", "type": "number"},
                    },
                    "properties": {"x": {"$ref": "#/$defs/A"}},
                },
            },
        ]
        resolve_tool_schema_refs(tools)
        prop_x = tools[0]["inputSchema"]["properties"]["x"]
        assert "type" in prop_x

    def test_deeply_nested_ref_resolves(self) -> None:
        tools = [
            {
                "inputSchema": {
                    "$defs": {
                        "A": {"$ref": "#/$defs/B"},
                        "B": {"$ref": "#/$defs/C"},
                        "C": {"type": "boolean", "title": "C"},
                    },
                    "properties": {"x": {"$ref": "#/$defs/A"}},
                },
            },
        ]
        resolve_tool_schema_refs(tools)
        prop_x = tools[0]["inputSchema"]["properties"]["x"]
        assert prop_x["type"] == "boolean"
        assert prop_x["title"] == "C"

    def test_merges_all_merge_keys(self) -> None:
        tools = [
            {
                "inputSchema": {
                    "$defs": {
                        "Foo": {
                            "type": "string",
                            "title": "Foo",
                            "description": "desc",
                            "default": "abc",
                            "enum": ["a", "b"],
                            "oneOf": [{"type": "string"}],
                            "anyOf": [{"type": "number"}],
                            "items": {"type": "string"},
                            "properties": {"nested": {"type": "integer"}},
                            "required": ["nested"],
                        },
                    },
                    "properties": {"x": {"$ref": "#/$defs/Foo"}},
                },
            },
        ]
        resolve_tool_schema_refs(tools)
        prop_x = tools[0]["inputSchema"]["properties"]["x"]
        assert prop_x["type"] == "string"
        assert prop_x["title"] == "Foo"
        assert prop_x["description"] == "desc"
        assert prop_x["default"] == "abc"
        assert prop_x["enum"] == ["a", "b"]
        assert prop_x["oneOf"] == [{"type": "string"}]
        assert prop_x["anyOf"] == [{"type": "number"}]
        assert prop_x["items"] == {"type": "string"}
        assert prop_x["properties"] == {"nested": {"type": "integer"}}
        assert prop_x["required"] == ["nested"]

    def test_handles_empty_tools_list(self) -> None:
        tools: list[dict[str, Any]] = []
        resolve_tool_schema_refs(tools)
        assert tools == []

    def test_handles_tool_without_input_schema(self) -> None:
        tools = [{"name": "test"}]
        resolve_tool_schema_refs(tools)
        assert tools == [{"name": "test"}]

    def test_handles_non_dict_input_schema(self) -> None:
        tools = [{"name": "test", "inputSchema": "not a dict"}]
        resolve_tool_schema_refs(tools)
        assert tools[0]["inputSchema"] == "not a dict"
