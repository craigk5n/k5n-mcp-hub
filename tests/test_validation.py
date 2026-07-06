import pytest

from mcp_hub.mcp.validation import validate_tool_name, validate_tool_schemas


class TestValidateToolName:
    def test_valid_name_returns_empty(self) -> None:
        result = validate_tool_name("foo_bar-1")
        assert result == []

    def test_empty_string_returns_is_empty(self) -> None:
        result = validate_tool_name("")
        assert result == ["is empty"]

    def test_uppercase_letter_returns_invalid_char(self) -> None:
        result = validate_tool_name("A")
        assert result == ["contains invalid character 'A'"]

    def test_space_returns_contains_spaces(self) -> None:
        result = validate_tool_name("a b")
        assert result == ["contains spaces"]

    def test_valid_alphanumeric_with_underscore_dash_dot(self) -> None:
        result = validate_tool_name("valid_name-1.0")
        assert result == []

    def test_long_name_returns_length_issue(self) -> None:
        long_name = "a" * 65
        result = validate_tool_name(long_name)
        assert result == ["is longer than 64 chars (65)"]

    def test_special_characters_return_invalid(self) -> None:
        result = validate_tool_name("test@tool")
        assert result == ["contains invalid character '@'"]

    def test_multiple_invalid_chars(self) -> None:
        result = validate_tool_name("a@b#c")
        assert result == [
            "contains invalid character '@'",
            "contains invalid character '#'",
        ]


class TestValidateToolSchemas:
    def test_valid_tool_returns_conformant(self) -> None:
        tool = {
            "name": "x",
            "inputSchema": {
                "type": "object",
                "properties": {"a": {}},
                "required": ["a"],
            },
        }
        is_conformant, issues = validate_tool_schemas([tool])
        assert is_conformant is True
        assert issues == []

    def test_missing_name_returns_issue(self) -> None:
        tool = {"inputSchema": {"type": "object"}}
        is_conformant, issues = validate_tool_schemas([tool])
        assert is_conformant is False
        assert "tool[0] missing name" in issues

    def test_required_field_not_in_properties_returns_issue(self) -> None:
        tool = {
            "name": "test",
            "inputSchema": {
                "type": "object",
                "properties": {"a": {}},
                "required": ["b"],
            },
        }
        is_conformant, issues = validate_tool_schemas([tool])
        assert is_conformant is False
        assert 'test required field "b" missing from properties' in issues

    def test_required_list_without_properties_returns_issue(self) -> None:
        tool = {
            "name": "test",
            "inputSchema": {"required": ["b"]},
        }
        is_conformant, issues = validate_tool_schemas([tool])
        assert is_conformant is False
        assert "test required list without properties" in issues

    def test_schema_type_not_object_with_properties_returns_issue(self) -> None:
        tool = {
            "name": "test",
            "inputSchema": {
                "type": "string",
                "properties": {"a": {}},
            },
        }
        is_conformant, issues = validate_tool_schemas([tool])
        assert is_conformant is False
        assert 'test schema type "string" with properties' in issues

    def test_invalid_tool_name_prefixes_issues(self) -> None:
        tool = {"name": "Bad Name", "inputSchema": {"type": "object"}}
        is_conformant, issues = validate_tool_schemas([tool])
        assert is_conformant is False
        assert "Bad Name name contains spaces" in issues
