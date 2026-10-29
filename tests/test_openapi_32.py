"""Story 13.10 -- OpenAPI 3.2.

3.2 is a minor release on 3.1's JSON Schema dialect, so a 3.2 spec maps exactly like
3.1. What it adds that does not fit one MCP tool call -- the QUERY method, custom
methods (`additionalOperations`), a whole-query-string parameter, and streaming
media (`itemSchema`) -- marks the operation unsupported, with a reason, rather than
being mapped wrongly. Its other additions are ignored without error.
"""

from __future__ import annotations

import json
import pathlib
import socket
from typing import Any

import pytest

from mcp_hub.models.openapi import OpenAPISnapshot
from mcp_hub.openapi.errors import SpecError
from mcp_hub.openapi.hints import operation_tags, security_scheme_hints, suggested_base_urls
from mcp_hub.openapi.isolation import SpecLimits, load_spec
from mcp_hub.openapi.loader import load_spec_unsandboxed
from mcp_hub.openapi.mapping import build_tools, preview
from mcp_hub.openapi.model import OperationKey
from mcp_hub.openapi.trimmed_spec import trimmed_spec

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "openapi" / "petstore-3.2.json"
MAX = 5 * 1024 * 1024


def _doc() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _mapped() -> dict[OperationKey, Any]:
    return {m.key: m for m in preview(_doc(), "3.2", overrides={}, description_limit=1024)}


class TestLoading:
    def test_32_loads_as_its_own_dialect(self):
        spec = load_spec_unsandboxed(FIXTURE.read_bytes(), max_bytes=MAX)
        assert spec.dialect == "3.2"

    async def test_through_the_isolated_loader(self):
        limits = SpecLimits(
            max_bytes=MAX,
            fetch_timeout_seconds=5,
            validate_timeout_seconds=60,
            memory_limit_mb=1024,
        )
        spec = await load_spec(FIXTURE.read_bytes(), limits=limits)
        assert spec.dialect == "3.2"
        assert (
            OpenAPISnapshot(
                document=spec.document, dialect=spec.dialect, sha256=spec.sha256
            ).dialect
            == "3.2"
        )

    @pytest.mark.parametrize("version", ["3.3.0", "3.10.1", "4.0.0"])
    def test_later_versions_are_refused_by_name(self, version):
        doc = {**_doc(), "openapi": version}
        with pytest.raises(SpecError) as excinfo:
            load_spec_unsandboxed(json.dumps(doc).encode(), max_bytes=MAX)
        assert version in excinfo.value.message
        assert "3.2" in excinfo.value.message  # says what is supported

    def test_an_invalid_32_document_is_refused(self):
        doc = _doc()
        doc["paths"]["/pets"]["get"]["responses"]["200"]["description"] = 5
        with pytest.raises(SpecError):
            load_spec_unsandboxed(json.dumps(doc).encode(), max_bytes=MAX)


class TestSelfDoesNotMoveRefResolution:
    """`$self` sets the document's base URI. With only `#/...` refs (everything else is
    refused before validation), nothing may be fetched or read because of it."""

    @pytest.fixture
    def no_network(self, monkeypatch):
        calls: list[Any] = []

        def spy(*args: Any, **kwargs: Any) -> Any:
            calls.append(args[0] if args else kwargs)
            raise OSError("network disabled in this test")

        monkeypatch.setattr(socket, "getaddrinfo", spy)
        return calls

    @pytest.mark.parametrize(
        "self_uri", ["https://specs.example.invalid/api.json", "http://169.254.169.254/x"]
    )
    def test_remote_self(self, no_network, self_uri):
        doc = {**_doc(), "$self": self_uri}
        assert load_spec_unsandboxed(json.dumps(doc).encode(), max_bytes=MAX).dialect == "3.2"
        assert no_network == []

    def test_file_self_is_never_read(self, tmp_path, no_network):
        secret = tmp_path / "secret.json"
        secret.write_text('{"marker": "the-secret-marker"}')
        doc = {**_doc(), "$self": secret.as_uri()}
        spec = load_spec_unsandboxed(json.dumps(doc).encode(), max_bytes=MAX)
        assert "the-secret-marker" not in json.dumps(spec.document)
        assert no_network == []

    def test_relative_refs_are_still_refused(self):
        doc = _doc()
        doc["paths"]["/pets"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ] = {"$ref": "other.json#/Pet"}
        with pytest.raises(SpecError, match="external"):
            load_spec_unsandboxed(json.dumps(doc).encode(), max_bytes=MAX)


class TestMapping:
    def test_ordinary_operations_map_as_31(self):
        mapped = _mapped()[OperationKey("GET", "/pets")]
        assert mapped.tool is not None and mapped.tool.name == "listPets"
        assert mapped.tool.input_schema["properties"]["limit"] == {"type": "integer"}
        assert mapped.tool.output_schema["properties"]["result"]["type"] == "array"

    @pytest.mark.parametrize(
        "key, reason",
        [
            (OperationKey("QUERY", "/pets"), "QUERY"),
            (OperationKey("COPY", "/pets"), "additionalOperations"),
            # The dedicated reason, not the generic "content serialization" one, which
            # would also refuse it (3.2 requires querystring parameters to use content).
            (OperationKey("GET", "/pets/search"), "in: querystring"),
            (OperationKey("GET", "/pets/events"), "itemSchema"),
        ],
    )
    def test_32_additions_are_unsupported_with_a_reason(self, key, reason):
        mapped = _mapped()[key]
        assert mapped.tool is None
        assert any(reason in r for r in mapped.unsupported), mapped.unsupported

    def test_a_streaming_request_body_is_unsupported(self):
        doc = _doc()
        doc["paths"]["/pets"]["post"] = {
            "requestBody": {"content": {"application/jsonl": {"itemSchema": {"type": "object"}}}},
            "responses": {"200": {"description": "ok"}},
        }
        mapped = {m.key: m for m in preview(doc, "3.2", overrides={}, description_limit=1024)}
        assert any("itemSchema" in r for r in mapped[OperationKey("POST", "/pets")].unsupported)

    def test_selecting_one_is_refused(self):
        from mcp_hub.openapi.model import MappingError

        with pytest.raises(MappingError, match="QUERY /pets"):
            build_tools(
                _doc(),
                "3.2",
                selection=frozenset({OperationKey("QUERY", "/pets")}),
                overrides={},
                description_limit=1024,
            )

    def test_tool_schemas_are_valid_2020_12(self):
        from jsonschema import Draft202012Validator

        for mapped in _mapped().values():
            if mapped.tool is not None:
                Draft202012Validator.check_schema(mapped.tool.input_schema)
                if mapped.tool.output_schema:
                    Draft202012Validator.check_schema(mapped.tool.output_schema)


class TestIgnoredAdditions:
    def _snap(self) -> OpenAPISnapshot:
        return OpenAPISnapshot(document=_doc(), dialect="3.2", sha256="a" * 64)

    def test_server_name_and_tag_hierarchy(self):
        assert suggested_base_urls(self._snap()) == ["https://petstore.example.com/v1"]
        assert operation_tags(self._snap())[OperationKey("GET", "/pets")] == ["dogs"]

    def test_device_flow_and_metadata_url_are_no_suggestion(self):
        (hint,) = security_scheme_hints(self._snap())
        assert hint["name"] == "oauth" and hint["suggest"] is None

    def test_the_trimmed_spec_drops_self_and_unselectable_operations(self):
        trimmed = trimmed_spec(_doc(), "3.2", frozenset({OperationKey("GET", "/pets")}))
        text = json.dumps(trimmed)
        assert "$self" not in text and "specs.example.com" not in text
        assert "searchPets" not in text and "copyPets" not in text
        assert trimmed["openapi"] == "3.2.0"
