import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app


class TestJinja2Environment:
    def test_app_state_templates_exists(self) -> None:
        app = create_app()
        assert hasattr(app.state, "templates")
        assert app.state.templates is not None

    def test_templates_is_jinja2_environment(self) -> None:
        from jinja2 import Environment

        app = create_app()
        assert isinstance(app.state.templates, Environment)


class TestHasFilter:
    def test_has_item_in_list(self) -> None:
        app = create_app()
        env = app.state.templates
        result = env.filters["has"]([1, 2, 3], 2)
        assert result is True

    def test_has_item_not_in_list(self) -> None:
        app = create_app()
        env = app.state.templates
        result = env.filters["has"]([1, 2, 3], 5)
        assert result is False

    def test_has_string_in_list(self) -> None:
        app = create_app()
        env = app.state.templates
        result = env.filters["has"](["a", "b", "c"], "b")
        assert result is True

    def test_has_in_empty_list(self) -> None:
        app = create_app()
        env = app.state.templates
        result = env.filters["has"]([], 1)
        assert result is False


class TestIconSrcFilter:
    def test_icon_src_returns_http_src(self) -> None:
        app = create_app()
        env = app.state.templates
        icons = [{"src": "http://example.com/icon.png"}]
        result = env.filters["icon_src"](icons)
        assert result == "http://example.com/icon.png"

    def test_icon_src_returns_https_src(self) -> None:
        app = create_app()
        env = app.state.templates
        icons = [{"src": "https://example.com/icon.png"}]
        result = env.filters["icon_src"](icons)
        assert result == "https://example.com/icon.png"

    def test_icon_src_returns_data_image_src(self) -> None:
        app = create_app()
        env = app.state.templates
        icons = [{"src": "data:image/png;base64,abc123"}]
        result = env.filters["icon_src"](icons)
        assert result == "data:image/png;base64,abc123"

    def test_icon_src_skips_relative_src(self) -> None:
        app = create_app()
        env = app.state.templates
        icons = [{"src": "/static/icon.png"}]
        result = env.filters["icon_src"](icons)
        assert result == ""

    def test_icon_src_skips_non_http_first_icon(self) -> None:
        app = create_app()
        env = app.state.templates
        icons = [{"src": "/static/icon.png"}, {"src": "https://example.com/icon.png"}]
        result = env.filters["icon_src"](icons)
        assert result == "https://example.com/icon.png"

    def test_icon_src_empty_list(self) -> None:
        app = create_app()
        env = app.state.templates
        result = env.filters["icon_src"]([])
        assert result == ""


class TestSchemaSummaryFilter:
    def test_schema_summary_with_title(self) -> None:
        app = create_app()
        env = app.state.templates
        variants = [{"title": "string", "type": "string"}]
        result = env.filters["schema_summary"](variants)
        assert result == "string"

    def test_schema_summary_with_type_only(self) -> None:
        app = create_app()
        env = app.state.templates
        variants = [{"type": "integer"}]
        result = env.filters["schema_summary"](variants)
        assert result == "integer"

    def test_schema_summary_appends_enum(self) -> None:
        app = create_app()
        env = app.state.templates
        variants = [{"title": "color", "enum": ["red", "blue"]}]
        result = env.filters["schema_summary"](variants)
        assert result == "color (enum)"

    def test_schema_summary_no_enum(self) -> None:
        app = create_app()
        env = app.state.templates
        variants = [{"title": "color"}]
        result = env.filters["schema_summary"](variants)
        assert result == "color"

    def test_schema_summary_multiple_variants(self) -> None:
        app = create_app()
        env = app.state.templates
        variants = [
            {"title": "string", "type": "string"},
            {"title": "color", "enum": ["red", "blue"]},
            {"type": "integer"},
        ]
        result = env.filters["schema_summary"](variants)
        assert result == "string, color (enum), integer"

    def test_schema_summary_empty_list(self) -> None:
        app = create_app()
        env = app.state.templates
        result = env.filters["schema_summary"]([])
        assert result == ""


class TestSchemaPropKeysFilter:
    def test_schema_prop_keys_returns_sorted_keys(self) -> None:
        app = create_app()
        env = app.state.templates
        props = {"z": 1, "a": 2, "m": 3}
        result = env.filters["schema_prop_keys"](props)
        assert result == ["a", "m", "z"]

    def test_schema_prop_keys_single_key(self) -> None:
        app = create_app()
        env = app.state.templates
        props = {"only": 1}
        result = env.filters["schema_prop_keys"](props)
        assert result == ["only"]

    def test_schema_prop_keys_empty_dict(self) -> None:
        app = create_app()
        env = app.state.templates
        result = env.filters["schema_prop_keys"]({})
        assert result == []


class TestPrettyJsonFilter:
    def test_pretty_json_valid_json(self) -> None:
        app = create_app()
        env = app.state.templates
        result = env.filters["pretty_json"]('{"key": "value"}')
        assert result == '{\n  "key": "value"\n}'

    def test_pretty_json_invalid_json_returns_unchanged(self) -> None:
        app = create_app()
        env = app.state.templates
        result = env.filters["pretty_json"]("not valid json")
        assert result == "not valid json"

    def test_pretty_json_nested_json(self) -> None:
        app = create_app()
        env = app.state.templates
        result = env.filters["pretty_json"]('{"a": {"b": 1}}')
        expected = '{\n  "a": {\n    "b": 1\n  }\n}'
        assert result == expected

    def test_pretty_json_array(self) -> None:
        app = create_app()
        env = app.state.templates
        result = env.filters["pretty_json"]("[1, 2, 3]")
        assert result == "[\n  1,\n  2,\n  3\n]"

    def test_pretty_json_malformed_json(self) -> None:
        app = create_app()
        env = app.state.templates
        result = env.filters["pretty_json"]('{"key": }')
        assert result == '{"key": }'
