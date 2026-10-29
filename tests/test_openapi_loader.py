"""Story 13.1 -- loading and validating OpenAPI documents (ADR 0011 §11).

A spec is untrusted input from someone else's URL or an admin's upload. These tests
pin the properties that make it safe to accept one: it parses into plain JSON data
and nothing else, its size is bounded however it is shaped, and **no `$ref` reaches
outside the document**. That last one is the security boundary, not a nicety:
`openapi-spec-validator` will follow an `http://` ref (bypassing the SSRF-pinned
transport) and read a `file://` one, echoing the file into its error message.
"""

from __future__ import annotations

import json
import pathlib
import socket
import time
from typing import Any

import httpx
import pytest

from mcp_hub.openapi.errors import SpecError
from mcp_hub.openapi.loader import (
    LoadedSpec,
    fetch_spec_bytes,
    load_spec_unsandboxed,
)
from mcp_hub.openapi.refs import decode_pointer, encode_pointer

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "openapi"
MAX = 5 * 1024 * 1024


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _spec_31(**extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {},
    }
    doc.update(extra)
    return doc


def _with_schema(schema: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """A 3.1 document whose single operation returns `schema`."""
    return _spec_31(
        paths={
            "/a": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "x",
                            "content": {"application/json": {"schema": schema}},
                        }
                    }
                }
            }
        },
        **extra,
    )


def _load(doc: dict[str, Any]) -> LoadedSpec:
    return load_spec_unsandboxed(json.dumps(doc).encode(), max_bytes=MAX)


def _error(doc_or_bytes: dict[str, Any] | bytes, *, max_bytes: int = MAX) -> SpecError:
    data = doc_or_bytes if isinstance(doc_or_bytes, bytes) else json.dumps(doc_or_bytes).encode()
    with pytest.raises(SpecError) as excinfo:
        load_spec_unsandboxed(data, max_bytes=max_bytes)
    return excinfo.value


# --------------------------------------------------------------------------- dialects


class TestDialects:
    def test_openapi_31_json(self):
        spec = load_spec_unsandboxed(_fixture("petstore-3.1.json"), max_bytes=MAX)
        assert spec.dialect == "3.1"
        assert spec.document["info"]["title"] == "Petstore"
        assert set(spec.document["paths"]) == {"/pets", "/pets/{petId}"}

    def test_openapi_30_yaml(self):
        spec = load_spec_unsandboxed(_fixture("petstore-3.0.yaml"), max_bytes=MAX)
        assert spec.dialect == "3.0"

    def test_swagger_20(self):
        spec = load_spec_unsandboxed(_fixture("petstore-swagger2.json"), max_bytes=MAX)
        assert spec.dialect == "2.0"

    def test_unquoted_swagger_version_in_yaml_is_accepted(self):
        # `swagger: 2.0` unquoted is a float in YAML and is everywhere in old specs.
        data = b"swagger: 2.0\ninfo: {title: t, version: '1'}\npaths: {}\n"
        spec = load_spec_unsandboxed(data, max_bytes=MAX)
        assert spec.dialect == "2.0"
        assert spec.document["swagger"] == "2.0"

    def test_openapi_32_is_refused_by_name(self):
        err = _error({**_spec_31(), "openapi": "3.2.0"})
        assert "3.2" in err.message
        assert "3.1" in err.message  # says what *is* supported

    @pytest.mark.parametrize(
        "doc",
        [
            {"info": {"title": "t", "version": "1"}, "paths": {}},
            {"openapi": "4.0.0", "info": {"title": "t", "version": "1"}, "paths": {}},
            {"swagger": "1.2", "info": {"title": "t", "version": "1"}},
            {"openapi": 3.1, "info": {"title": "t", "version": "1"}, "paths": {}},
        ],
    )
    def test_unrecognized_version_is_refused(self, doc):
        err = _error(doc)
        assert "version" in err.message.lower()

    @pytest.mark.parametrize("data", [b"[]", b'"just a string"', b"42", b"", b"   \n"])
    def test_document_must_be_a_mapping(self, data):
        err = _error(data)
        assert "object" in err.message.lower() or "empty" in err.message.lower()


# --------------------------------------------------------------------------- parsing


class TestParsing:
    def test_yaml_keys_keep_their_source_spelling(self):
        spec = load_spec_unsandboxed(_fixture("petstore-3.0.yaml"), max_bytes=MAX)
        responses = spec.document["paths"]["/pets/{petId}"]["get"]["responses"]
        # An unquoted `200:` is an int in YAML; JSON storage and every lookup want "200".
        assert list(responses) == ["200"]
        pet = spec.document["components"]["schemas"]["Pet"]["properties"]
        # `on:` is a boolean key in YAML 1.1. Here it is the property name it looks like.
        assert "on" in pet

    def test_yaml_dates_and_yes_no_stay_strings(self):
        spec = load_spec_unsandboxed(_fixture("petstore-3.0.yaml"), max_bytes=MAX)
        assert spec.document["info"]["version"] == "2024-01-01"
        enum = spec.document["components"]["schemas"]["Pet"]["properties"]["vaccinated"]["enum"]
        assert enum == ["yes", "no", "on", "off"]

    def test_yaml_true_false_are_still_booleans(self):
        spec = load_spec_unsandboxed(_fixture("petstore-3.0.yaml"), max_bytes=MAX)
        param = spec.document["paths"]["/pets/{petId}"]["get"]["parameters"][0]
        assert param["required"] is True

    def test_utf8_bom_is_accepted(self):
        spec = load_spec_unsandboxed(
            b"\xef\xbb\xbf" + json.dumps(_spec_31()).encode(), max_bytes=MAX
        )
        assert spec.dialect == "3.1"

    def test_invalid_utf8_is_refused(self):
        err = _error(b'{"openapi": "3.1.0", "info": {"title": "\xff"}}')
        assert "utf-8" in err.message.lower()

    @pytest.mark.parametrize(
        "data",
        [
            b'{"openapi": "3.1.0", "info": {"title": "t", "version": "1"}, "x": NaN}',
            b'{"openapi": "3.1.0", "info": {"title": "t", "version": "1"}, "x": Infinity}',
            b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\nx: .nan\n",
            b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\nx: -.inf\n",
        ],
    )
    def test_non_finite_numbers_are_refused(self, data):
        _error(data)

    @pytest.mark.parametrize(
        "data",
        [
            b'{"openapi": "3.1.0", "info": {"title": "a", "title": "b", "version": "1"}}',
            b"openapi: 3.1.0\ninfo:\n  title: a\n  title: b\n  version: '1'\n",
        ],
    )
    def test_duplicate_keys_are_refused(self, data):
        err = _error(data)
        assert "duplicate" in err.message.lower()
        assert "title" in err.message

    def test_yaml_merge_keys_are_not_duplicates(self):
        data = (
            b"openapi: 3.1.0\n"
            b"info: {title: t, version: '1'}\n"
            b"paths: {}\n"
            b"x-base: &base {a: 1, b: 2}\n"
            b"x-derived:\n  <<: *base\n  b: 3\n"
        )
        spec = load_spec_unsandboxed(data, max_bytes=MAX)
        assert spec.document["x-derived"] == {"a": 1, "b": 3}

    @pytest.mark.parametrize(
        "tagged",
        [
            b"x: !!binary aGVsbG8=\n",
            b"x: !!set {a, b}\n",
            b"x: !!timestamp 2024-01-01\n",
            b"x: !!python/object/apply:os.system ['true']\n",
            b"? [a, b]\n: complex key\n",
        ],
    )
    def test_non_json_yaml_is_refused(self, tagged):
        data = b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\n" + tagged
        _error(data)

    def test_yaml_syntax_error_reports_a_line(self):
        err = _error(b"openapi: 3.1.0\ninfo: [unclosed\n")
        assert "line" in err.message.lower()

    def test_json_syntax_error_reports_a_position(self):
        err = _error(b'{"openapi": "3.1.0",, }')
        assert "line" in err.message.lower()


class TestReviewFindingsParsing:
    """Security review of Story 13.1 (findings 2, 7, 8, 10)."""

    @pytest.mark.parametrize(
        "data",
        [
            b'{"openapi": "3.1.0", "info": {"title": "t", "version": "1"}, "paths": {}, "x-n": '
            + b"9" * 5000
            + b"}",
            b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\nx-n: " + b"9" * 5000,
        ],
        ids=["json", "yaml"],
    )
    def test_huge_integers_are_a_spec_error_not_a_crash(self, data):
        # Python refuses to parse an int over 4300 digits with a bare ValueError.
        assert "number" in _error(data).message.lower()

    def test_yaml_numbers_follow_yaml_12_not_11(self):
        data = (
            b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\n"
            b"x-v: [0x1F, 1_000, 1:30, 012, 0b11, 0o17, +12, -3, 1.5, 1e3, 2.]\n"
        )
        values = load_spec_unsandboxed(data, max_bytes=MAX).document["x-v"]
        # YAML 1.1 read these as 31, 1000, 90 (base 60), 10 (octal) and 3. An enum or
        # default silently changing value is worse than a string staying a string.
        assert values == ["0x1F", "1_000", "1:30", 12, "0b11", "0o17", 12, -3, 1.5, 1000.0, 2.0]

    def test_explicitly_tagged_non_decimal_int_is_refused(self):
        _error(b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\nx: !!int 0x1F\n")

    @pytest.mark.parametrize(
        "tagged, type_name",
        [
            (b"x: !!binary aGVsbG8=\n", "bytes"),
            (b"x: !!set {a, b}\n", "set"),
            (b"x: !!timestamp 2024-01-01\n", "date"),
            (b"x: !!timestamp 2024-01-01T00:00:00Z\n", "datetime"),
            (b"x: !!omap [{a: 1}]\n", "tuple"),
        ],
    )
    def test_non_json_values_are_refused_by_the_scalar_check(self, tagged, type_name):
        # Pins the message, so the refusal is known to come from the plain-JSON check
        # and not from something downstream tripping over the value.
        data = b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\n" + tagged
        err = _error(data)
        assert f"a {type_name} value" in err.message
        assert err.pointer.startswith("/x")

    def test_merge_bomb_is_linear(self):
        # Each level merges five aliases of the level below. PyYAML's node-level
        # flattening re-expands every one, so twelve levels took minutes.
        lines = [
            "openapi: 3.1.0",
            "info: {title: t, version: '1'}",
            "paths: {}",
            "x-m0: &m0 {k: 1}",
        ]
        for i in range(1, 30):
            sources = ", ".join([f"*m{i - 1}"] * 5)
            lines.append(f"x-m{i}: &m{i} {{<<: [{sources}]}}")
        start = time.monotonic()
        spec = load_spec_unsandboxed("\n".join(lines).encode(), max_bytes=MAX)
        assert time.monotonic() - start < 2
        assert spec.document["x-m29"] == {"k": 1}

    def test_merge_precedence(self):
        data = (
            b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\n"
            b"x-a: &a {k: 1, own: a}\n"
            b"x-b: &b {k: 2, j: 3, own: b}\n"
            b"x-m: {<<: [*a, *b], own: m}\n"
        )
        # Earlier sources win over later ones, and the mapping's own keys win over all.
        doc = load_spec_unsandboxed(data, max_bytes=MAX).document
        assert doc["x-m"] == {"k": 1, "j": 3, "own": "m"}

    def test_nested_merges(self):
        data = (
            b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\n"
            b"x-a: &a {a: 1}\n"
            b"x-b: &b {<<: *a, b: 2}\n"
            b"x-c: {<<: *b, c: 3}\n"
        )
        assert load_spec_unsandboxed(data, max_bytes=MAX).document["x-c"] == {
            "a": 1,
            "b": 2,
            "c": 3,
        }

    def test_merge_into_a_later_anchor_sees_its_keys(self):
        # The anchor sits deeper in an earlier sibling, so a construct-then-merge
        # approach would see it still empty. Node-level merging does not.
        data = (
            b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\n"
            b"x-a: {deep: &x {k: 1}}\n"
            b"x-c: {<<: *x, j: 2}\n"
        )
        assert load_spec_unsandboxed(data, max_bytes=MAX).document["x-c"] == {"k": 1, "j": 2}

    def test_self_merge_is_refused(self):
        data = b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\nx: &a {<<: *a, k: 1}\n"
        assert "merge" in _error(data).message.lower()

    @pytest.mark.parametrize("source", [b"1", b"[1, 2]", b"text"])
    def test_merge_source_must_be_a_mapping(self, source):
        data = (
            b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\nx: {<<: " + source + b"}\n"
        )
        assert "merge" in _error(data).message.lower()


# --------------------------------------------------------------------------- bounds


class TestBounds:
    def test_oversized_document_is_refused_before_parsing(self):
        data = json.dumps(_spec_31()).encode()
        err = _error(data, max_bytes=len(data) - 1)
        assert "too large" in err.message.lower()

    def test_yaml_alias_bomb_is_refused_quickly(self):
        # "Billion laughs": nine levels of ten aliases each is 10**9 strings once
        # expanded, from a few hundred bytes. Parsing shares the objects, so it is the
        # walk over them that has to stop -- and it has to stop early.
        lines = ["openapi: 3.1.0", "info: {title: t, version: '1'}", "paths: {}", "a0: &a0 lol"]
        for i in range(1, 10):
            prev = f"*a{i - 1}"
            lines.append(f"a{i}: &a{i} [{', '.join([prev] * 10)}]")
        data = "\n".join(lines).encode()
        start = time.monotonic()
        err = _error(data)
        assert time.monotonic() - start < 5
        assert "too large" in err.message.lower() or "too many" in err.message.lower()

    def test_self_containing_yaml_is_refused(self):
        # `&a [*a]` builds a list that contains itself. It cannot be stored as JSON,
        # and a walk that waits for its children to finish would never finish.
        data = b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\nx: &a [*a]\n"
        err = _error(data)
        assert "nested" in err.message.lower()

    def test_shared_objects_count_once_per_use(self, monkeypatch: pytest.MonkeyPatch):
        # Memoizing must not under-count: twenty uses of one 10-value list is ~200.
        import mcp_hub.openapi.parse as parse

        monkeypatch.setattr(parse, "MAX_NODES", 150)
        uses = ", ".join(["*l"] * 20)
        data = (
            b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\npaths: {}\n"
            b"x-list: &l [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]\n"
            b"x-uses: [" + uses.encode() + b"]\n"
        )
        assert "too large" in _error(data).message.lower()

    def test_deep_json_nesting_is_refused_not_a_crash(self):
        deep = b"[" * 100_000 + b"]" * 100_000
        data = b'{"openapi": "3.1.0", "info": {"title": "t", "version": "1"}, "x": ' + deep + b"}"
        err = _error(data)
        assert "nested" in err.message.lower()

    def test_deep_yaml_nesting_is_refused_not_a_crash(self):
        deep = b"[" * 5_000 + b"]" * 5_000
        data = b"openapi: 3.1.0\ninfo: {title: t, version: '1'}\nx: " + deep + b"\n"
        err = _error(data)
        assert "nested" in err.message.lower()


# --------------------------------------------------------------------------- validation


class TestValidation:
    def test_validation_error_names_the_json_pointer(self):
        err = _error({**_spec_31(), "info": {"title": 1, "version": "1"}})
        assert err.pointer == "/info/title"
        assert "string" in err.message

    def test_pointer_escapes_slashes_in_path_keys(self):
        doc = _spec_31(paths={"/pets/{id}": {"get": {"responses": {"200": {"description": 5}}}}})
        err = _error(doc)
        assert err.pointer.startswith("/paths/~1pets~1{id}/get")

    def test_error_messages_are_capped(self):
        # Validator messages echo document values; a hostile spec must not be able to
        # push a megabyte of itself into the admin UI through an error.
        from mcp_hub.openapi.errors import MAX_MESSAGE_CHARS

        err = _error({**_spec_31(), "info": {"title": ["x" * 100_000], "version": "1"}})
        assert len(err.message) <= MAX_MESSAGE_CHARS

    def test_invalid_30_document(self):
        doc = {"openapi": "3.0.3", "info": {"title": "t"}, "paths": {}}
        err = _error(doc)
        assert "version" in err.message

    def test_invalid_swagger_document(self):
        doc = {"swagger": "2.0", "info": {"title": "t", "version": "1"}}
        err = _error(doc)
        assert "paths" in err.message


# --------------------------------------------------------------------------- refs


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record any DNS lookup. A loader that only looks inside the document makes none."""
    calls: list[Any] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[0] if args else kwargs)
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket, "getaddrinfo", spy)
    return calls


class TestRefs:
    def test_local_refs_resolve(self, no_network):
        spec = load_spec_unsandboxed(_fixture("petstore-3.1.json"), max_bytes=MAX)
        assert spec.dialect == "3.1"
        assert no_network == []

    def test_refs_are_left_in_place_not_inlined(self):
        # ADR 0011 §2: inlining lets one reused or recursive schema multiply the size.
        spec = load_spec_unsandboxed(_fixture("petstore-3.1.json"), max_bytes=MAX)
        schema = spec.document["paths"]["/pets/{petId}"]["get"]["responses"]["200"]
        assert schema["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/Pet"
        }

    @pytest.mark.parametrize(
        "ref",
        [
            "http://example.invalid/schema.json",
            "https://example.invalid/schema.json#/Pet",
            "//example.invalid/schema.json",
            "other.yaml#/components/schemas/Pet",
            "other.yaml",
            "#Pet",  # an anchor, not a pointer; resolving it needs $anchor support
            "#",
        ],
    )
    def test_non_pointer_refs_are_refused_without_network(self, ref, no_network):
        err = _error(_with_schema({"$ref": ref}))
        assert ref in err.message
        assert err.pointer.endswith("/schema/$ref")
        assert no_network == []

    def test_file_ref_is_refused_and_never_read(self, tmp_path):
        secret = tmp_path / "secret.json"
        secret.write_text('"the-secret-marker"')
        err = _error(_with_schema({"$ref": secret.as_uri()}))
        assert "the-secret-marker" not in err.message
        assert "external" in err.message.lower()

    def test_external_ref_in_a_path_item_is_refused(self, no_network):
        doc = _spec_31(paths={"/a": {"$ref": "https://example.invalid/paths.json#/a"}})
        err = _error(doc)
        assert err.pointer == "/paths/~1a/$ref"
        assert no_network == []

    @pytest.mark.parametrize("keyword", ["$id", "$dynamicRef", "$recursiveRef"])
    def test_rebasing_and_dynamic_keywords_are_refused(self, keyword, no_network):
        # `$id` changes the base URI, so a relative `$ref` beneath it would resolve
        # against an attacker-chosen host.
        err = _error(_with_schema({keyword: "https://example.invalid/base", "type": "object"}))
        assert keyword in err.message
        assert no_network == []

    def test_a_property_named_ref_is_not_a_ref(self):
        schema = {"type": "object", "properties": {"$ref": {"type": "string"}}}
        spec = _load(_with_schema(schema))
        assert spec.dialect == "3.1"

    def test_dangling_ref_names_the_ref_and_its_target(self):
        err = _error(_with_schema({"$ref": "#/components/schemas/Nope"}))
        assert "#/components/schemas/Nope" in err.message
        assert err.pointer.endswith("/schema/$ref")

    def test_ref_into_a_list_by_index(self):
        doc = _with_schema(
            {"$ref": "#/x-list/1"},
            **{"x-list": [{"type": "string"}, {"type": "integer"}]},
        )
        assert _load(doc).dialect == "3.1"

    def test_ref_with_escaped_and_percent_encoded_pointer(self):
        doc = _with_schema(
            {"$ref": "#/x-defs/a~1b/c%20d"},
            **{"x-defs": {"a/b": {"c d": {"type": "string"}}}},
        )
        assert _load(doc).dialect == "3.1"

    def test_ref_to_a_non_object_is_refused(self):
        doc = _with_schema({"$ref": "#/info/title"})
        err = _error(doc)
        assert "#/info/title" in err.message

    def test_recursive_schema_through_properties_is_fine(self):
        doc = _with_schema(
            {"$ref": "#/components/schemas/Node"},
            components={
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
            },
        )
        assert _load(doc).dialect == "3.1"

    def test_pure_ref_cycle_is_refused_not_a_crash(self):
        doc = _with_schema(
            {"$ref": "#/components/schemas/A"},
            components={
                "schemas": {
                    "A": {"$ref": "#/components/schemas/B"},
                    "B": {"$ref": "#/components/schemas/A"},
                }
            },
        )
        err = _error(doc)
        assert "cycle" in err.message.lower()
        assert "#/components/schemas/A" in err.message
        assert "#/components/schemas/B" in err.message

    @pytest.mark.parametrize("keyword", ["allOf", "anyOf", "oneOf"])
    def test_composition_cycle_is_refused_not_a_crash(self, keyword):
        doc = _with_schema(
            {"$ref": "#/components/schemas/A"},
            components={"schemas": {"A": {keyword: [{"$ref": "#/components/schemas/A"}]}}},
        )
        err = _error(doc)
        assert "cycle" in err.message.lower()

    def test_cycle_through_not(self):
        doc = _with_schema(
            {"$ref": "#/components/schemas/A"},
            components={
                "schemas": {
                    "A": {"not": {"$ref": "#/components/schemas/B"}},
                    "B": {"allOf": [{"$ref": "#/components/schemas/A"}]},
                }
            },
        )
        assert "cycle" in _error(doc).message.lower()


class TestRefFanOut:
    """Security review finding 3: an acyclic but branching composition chain."""

    def _fan_out(self, levels: int) -> dict[str, Any]:
        schemas: dict[str, Any] = {
            f"A{i}": {"allOf": [{"$ref": f"#/components/schemas/A{i + 1}"}] * 2}
            for i in range(levels)
        }
        schemas[f"A{levels}"] = {"type": "string"}
        return _with_schema({"$ref": "#/components/schemas/A0"}, components={"schemas": schemas})

    def test_exponential_composition_is_refused_before_validation(self):
        # 2**40 expansions: the validator would never finish this.
        start = time.monotonic()
        err = _error(self._fan_out(40))
        assert time.monotonic() - start < 2
        assert "expands" in err.message

    def test_modest_reuse_is_fine(self):
        assert _load(self._fan_out(6)).dialect == "3.1"


class TestPointers:
    @pytest.mark.parametrize(
        "parts, pointer",
        [
            ((), ""),
            (("info", "title"), "/info/title"),
            (("paths", "/pets/{id}", "get"), "/paths/~1pets~1{id}/get"),
            (("x", "a~b"), "/x/a~0b"),
            (("tags", 0), "/tags/0"),
        ],
    )
    def test_round_trip(self, parts, pointer):
        assert encode_pointer(parts) == pointer
        assert decode_pointer(pointer) == [str(p) for p in parts]


# --------------------------------------------------------------------------- identity


class TestSha256:
    def test_is_hex_sha256(self):
        spec = load_spec_unsandboxed(_fixture("petstore-3.1.json"), max_bytes=MAX)
        assert len(spec.sha256) == 64
        int(spec.sha256, 16)

    def test_formatting_does_not_change_it(self):
        # ADR 0011 §2: a revision exists only when the SHA-256 differs, so re-indenting
        # or re-serializing a spec must not look like a change.
        doc = json.loads(_fixture("petstore-3.1.json"))
        compact = load_spec_unsandboxed(
            json.dumps(doc, separators=(",", ":")).encode(), max_bytes=MAX
        )
        pretty = load_spec_unsandboxed(
            json.dumps(doc, indent=4, sort_keys=True).encode(), max_bytes=MAX
        )
        import yaml

        as_yaml = load_spec_unsandboxed(yaml.safe_dump(doc).encode(), max_bytes=MAX)
        assert compact.sha256 == pretty.sha256 == as_yaml.sha256

    def test_content_changes_it(self):
        doc = json.loads(_fixture("petstore-3.1.json"))
        before = _load(doc).sha256
        doc["info"]["title"] = "Petstore 2"
        assert _load(doc).sha256 != before


# --------------------------------------------------------------------------- fetching


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _streamed(body: bytes):
    """A streamed body, as a real server sends one. `httpx.Response(content=bytes)`
    is read (and decoded) eagerly, so it cannot exercise raw streaming."""

    async def chunks():
        yield body

    return chunks()


class TestFetch:
    async def test_fetches_the_body(self):
        body = _fixture("petstore-3.1.json")
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=_streamed(body))

        async with _client(handler) as client:
            data = await fetch_spec_bytes(
                "https://api.example.com/openapi.json",
                max_bytes=MAX,
                timeout_seconds=5,
                allow_private_networks=False,
                http_client=client,
            )
        assert data == body
        assert seen[0].method == "GET"

    async def test_declared_oversize_is_refused(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 10, headers={"Content-Length": str(MAX + 1)})

        async with _client(handler) as client:
            with pytest.raises(SpecError, match="(?i)too large"):
                await fetch_spec_bytes(
                    "https://api.example.com/s.json",
                    max_bytes=MAX,
                    timeout_seconds=5,
                    allow_private_networks=False,
                    http_client=client,
                )

    async def test_undeclared_oversize_stops_streaming(self):
        chunks_sent: list[int] = []

        async def stream():
            for _ in range(1000):
                chunks_sent.append(1)
                yield b"x" * 1024

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=stream())

        async with _client(handler) as client:
            with pytest.raises(SpecError, match="(?i)too large"):
                await fetch_spec_bytes(
                    "https://api.example.com/s.json",
                    max_bytes=10 * 1024,
                    timeout_seconds=5,
                    allow_private_networks=False,
                    http_client=client,
                )
        assert len(chunks_sent) < 1000

    async def test_asks_for_an_uncompressed_body(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=_streamed(b"{}"))

        async with _client(handler) as client:
            await fetch_spec_bytes(
                "https://api.example.com/s.json",
                max_bytes=MAX,
                timeout_seconds=5,
                allow_private_networks=False,
                http_client=client,
            )
        assert seen[0].headers["accept-encoding"] == "identity"

    @pytest.mark.parametrize("encoding", ["gzip", "gzip, gzip", "br", "deflate"])
    async def test_a_compressed_body_is_refused(self, encoding):
        # Security review finding 6: decoding happens before any size check could
        # see it, so 5 KB of stacked gzip became 400 MB in one chunk.
        import gzip

        body = gzip.compress(gzip.compress(b" " * 1_000_000))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=_streamed(body), headers={"Content-Encoding": encoding}
            )

        async with _client(handler) as client:
            with pytest.raises(SpecError, match="(?i)compressed"):
                await fetch_spec_bytes(
                    "https://api.example.com/s.json",
                    max_bytes=MAX,
                    timeout_seconds=5,
                    allow_private_networks=False,
                    http_client=client,
                )

    async def test_identity_encoding_is_accepted(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=_streamed(b"{}"), headers={"Content-Encoding": "identity"}
            )

        async with _client(handler) as client:
            data = await fetch_spec_bytes(
                "https://api.example.com/s.json",
                max_bytes=MAX,
                timeout_seconds=5,
                allow_private_networks=False,
                http_client=client,
            )
        assert data == b"{}"

    async def test_redirect_is_not_followed(self):
        hits: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            hits.append(str(request.url))
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})

        async with _client(handler) as client:
            with pytest.raises(SpecError) as excinfo:
                await fetch_spec_bytes(
                    "https://api.example.com/s.json",
                    max_bytes=MAX,
                    timeout_seconds=5,
                    allow_private_networks=False,
                    http_client=client,
                )
        assert "169.254.169.254" in excinfo.value.message
        assert hits == ["https://api.example.com/s.json"]

    async def test_error_status_is_reported(self):
        async with _client(lambda r: httpx.Response(404)) as client:
            with pytest.raises(SpecError, match="404"):
                await fetch_spec_bytes(
                    "https://api.example.com/s.json",
                    max_bytes=MAX,
                    timeout_seconds=5,
                    allow_private_networks=False,
                    http_client=client,
                )

    async def test_transport_error_is_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        async with _client(handler) as client:
            with pytest.raises(SpecError, match="(?i)could not fetch"):
                await fetch_spec_bytes(
                    "https://api.example.com/s.json",
                    max_bytes=MAX,
                    timeout_seconds=5,
                    allow_private_networks=False,
                    http_client=client,
                )

    async def test_slow_body_hits_the_overall_deadline(self):
        import asyncio

        async def dribble():
            for _ in range(100):
                await asyncio.sleep(0.05)
                yield b" "

        async with _client(lambda r: httpx.Response(200, content=dribble())) as client:
            start = time.monotonic()
            with pytest.raises(SpecError, match="(?i)timed out"):
                await fetch_spec_bytes(
                    "https://api.example.com/s.json",
                    max_bytes=MAX,
                    timeout_seconds=0.3,
                    allow_private_networks=False,
                    http_client=client,
                )
        assert time.monotonic() - start < 2

    @pytest.mark.parametrize(
        "url",
        ["file:///etc/passwd", "ftp://example.com/s.json", "gopher://x/", "not a url", "https://"],
    )
    async def test_only_http_urls_are_fetched(self, url):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("must not be called")

        async with _client(handler) as client:
            with pytest.raises(SpecError, match="(?i)url"):
                await fetch_spec_bytes(
                    url,
                    max_bytes=MAX,
                    timeout_seconds=5,
                    allow_private_networks=False,
                    http_client=client,
                )

    async def test_default_client_is_ssrf_pinned(self):
        # No client injected: the hub's pinned transport refuses loopback unless the
        # operator allowed private networks. Nothing listens on port 9; the refusal
        # comes from the guard, before any connection.
        # Matching the guard's own wording, so a plain "connection refused" from port 9
        # cannot make this pass.
        with pytest.raises(SpecError, match="SSRF validation"):
            await fetch_spec_bytes(
                "http://127.0.0.1:9/openapi.json",
                max_bytes=MAX,
                timeout_seconds=5,
                allow_private_networks=False,
            )
