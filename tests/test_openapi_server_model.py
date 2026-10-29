"""Story 13.4 (part A) -- an OpenAPI backend as a `RegisteredServer` record.

Everything lives inline on the record (ADR 0011 §2), so it persists to
`mcp_servers.json` like any other server, and records written before this story load
unchanged. A static API key joins the credential types, redacted wherever
`bearer_token` is.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import httpx
import pytest

from mcp_hub.auth.caller import SERVICE_IDENTITY
from mcp_hub.mcp.auth import apply_server_auth
from mcp_hub.models import RegisteredServer
from mcp_hub.models.openapi import OpenAPIOperationRef, OpenAPIOverride, OpenAPISnapshot
from mcp_hub.openapi.server_tools import openapi_tools
from mcp_hub.storage.jsonfile import JSONFileStorage
from mcp_hub.trace.recorder import redact_query_params, sanitize_trace_headers

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "openapi"


def _snapshot() -> OpenAPISnapshot:
    document = json.loads((FIXTURES / "petstore-3.1.json").read_text())
    return OpenAPISnapshot(document=document, dialect="3.1", sha256="a" * 64, source_url="")


def _server(**fields: Any) -> RegisteredServer:
    values: dict[str, Any] = {
        "id": "pets",
        "url": "",
        "transport_kind": "openapi",
        "openapi_base_url": "https://petstore.example.com/v1",
        "openapi_snapshot": _snapshot(),
        "openapi_selection": [
            OpenAPIOperationRef(method="GET", path="/pets"),
            OpenAPIOperationRef(method="post", path="/pets"),
        ],
        "openapi_overrides": [
            OpenAPIOverride(
                method="GET", path="/pets", name="list_all_pets", description="All of them"
            )
        ],
        "openapi_health_probe": OpenAPIOperationRef(method="GET", path="/pets"),
    }
    values.update(fields)
    return RegisteredServer(**values)


class TestRecord:
    def test_url_is_synthesized(self):
        server = _server()
        assert server.url == "openapi:pets"
        assert server.is_openapi and not server.is_stdio

    def test_synthetic_url_replaces_anything_supplied(self):
        # The base URL is a separate field; `url` must never be taken for it, because
        # ~43 call sites treat `url` as an MCP endpoint.
        assert _server(url="https://evil.example.com/mcp").url == "openapi:pets"

    def test_operation_refs_normalize_the_method(self):
        assert _server().openapi_selection[1].method == "POST"

    def test_mcp_transport_accepts_openapi(self):
        assert _server(mcp_transport="openapi").mcp_transport == "openapi"

    def test_records_from_before_this_story_load_unchanged(self):
        old = {
            "id": "legacy",
            "url": "https://mcp.example.com/mcp",
            "auth_type": "bearer",
            "bearer_token": "t",
            "mcp_transport": "sse",
        }
        server = RegisteredServer.model_validate(old)
        assert server.transport_kind == "http"
        assert server.url == old["url"]
        assert server.openapi_snapshot is None
        assert server.openapi_selection == []
        assert not server.is_openapi


class TestPersistence:
    async def test_json_round_trip_gives_identical_tools(self, tmp_path):
        path = tmp_path / "servers.json"
        storage = JSONFileStorage(str(path))
        await storage.init()
        original = _server()
        await storage.register(original.sanitize_for_persistence())
        before = [t.definition.to_mcp() for t in openapi_tools(original, description_limit=1024)]

        reloaded_storage = JSONFileStorage(str(path))
        await reloaded_storage.init()
        reloaded = await reloaded_storage.get("pets")
        assert reloaded is not None
        after = [t.definition.to_mcp() for t in openapi_tools(reloaded, description_limit=1024)]

        assert after == before
        assert [t["name"] for t in after] == ["list_all_pets", "createPet"]
        assert reloaded.openapi_health_probe == OpenAPIOperationRef(method="GET", path="/pets")

    def test_persistence_keeps_the_snapshot_and_choices(self):
        server = _server(openapi_pending=_snapshot())
        kept = server.sanitize_for_persistence()
        assert kept.openapi_snapshot == server.openapi_snapshot
        assert kept.openapi_pending == server.openapi_pending
        assert kept.openapi_selection == server.openapi_selection
        assert kept.openapi_overrides == server.openapi_overrides

    def test_no_snapshot_means_no_tools(self):
        assert openapi_tools(_server(openapi_snapshot=None), description_limit=1024) == ()


API_KEY = {
    "auth_type": "api_key",
    "api_key_name": "X-Pet-Key",
    "api_key_in": "header",
    "api_key_value": "s3cret",
}


class TestApiKeyRedaction:
    @pytest.mark.parametrize("method", ["sanitize_for_ui", "sanitize_for_api"])
    def test_blanked_like_bearer_token(self, method):
        server = _server(**API_KEY, bearer_token="b")
        clean = getattr(server, method)()
        assert clean.api_key_value == ""
        assert clean.bearer_token == ""
        assert clean.api_key_name == "X-Pet-Key"  # not secret: an operator needs it

    def test_kept_for_persistence(self):
        assert _server(**API_KEY).sanitize_for_persistence().api_key_value == "s3cret"

    def test_counts_as_a_service_credential(self):
        assert _server(**API_KEY).has_service_credential

    def test_custom_header_is_redacted_in_traces(self):
        headers = {"X-Pet-Key": "s3cret", "Accept": "application/json"}
        clean = sanitize_trace_headers(headers, extra_sensitive=["x-pet-key"])
        assert clean["X-Pet-Key"] != "s3cret"
        assert clean["Accept"] == "application/json"

    def test_query_key_is_redacted_in_urls(self):
        url = "https://api.example.com/pets?limit=5&key=s3cret&key=again"
        clean = redact_query_params(url, ["key"])
        assert "s3cret" not in clean and "again" not in clean
        assert "limit=5" in clean

    def test_redact_query_leaves_other_urls_alone(self):
        assert redact_query_params("https://a.example/p?x=1", ["key"]) == "https://a.example/p?x=1"
        assert redact_query_params("https://a.example/p", []) == "https://a.example/p"


class TestApplyServerAuth:
    async def test_header_key(self):
        headers: dict[str, str] = {}
        await apply_server_auth(headers, _server(**API_KEY), caller=SERVICE_IDENTITY)
        assert headers == {"X-Pet-Key": "s3cret"}

    async def test_query_key_goes_to_params(self):
        headers: dict[str, str] = {}
        params: dict[str, str] = {}
        server = _server(**{**API_KEY, "api_key_in": "query", "api_key_name": "key"})
        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY, params=params)
        assert params == {"key": "s3cret"}
        assert headers == {}

    async def test_query_key_without_a_params_target_fails_loudly(self):
        server = _server(**{**API_KEY, "api_key_in": "query", "api_key_name": "key"})
        with pytest.raises(ValueError, match="query"):
            await apply_server_auth({}, server, caller=SERVICE_IDENTITY)

    async def test_bearer_to_a_rest_api_sends_no_mcp_token_header(self):
        # X-MCP-Token is an MCP-server convention for Apache/PHP hosts that strip
        # Authorization. Sent to an arbitrary REST API it is a second copy of the
        # credential, so an OpenAPI backend gets Authorization alone.
        headers: dict[str, str] = {}
        await apply_server_auth(
            headers, _server(auth_type="bearer", bearer_token="t"), caller=SERVICE_IDENTITY
        )
        assert headers == {"Authorization": "Bearer t"}

    async def test_bearer_to_an_mcp_server_is_unchanged(self):
        headers = httpx.Headers()
        mcp = RegisteredServer(
            id="m", url="https://mcp.example.com", auth_type="bearer", bearer_token="t"
        )
        await apply_server_auth(headers, mcp, caller=SERVICE_IDENTITY)
        assert headers["X-MCP-Token"] == "t"


class TestReviewFixes:
    """Security review of Story 13.4 (findings 1, 6)."""

    @pytest.mark.parametrize("method", ["sanitize_for_ui", "sanitize_for_api"])
    def test_spec_documents_are_not_echoed(self, method):
        # Up to max_spec_bytes per snapshot, with example tokens and internal hosts in
        # them, in every server listing: summaries only.
        server = _server(openapi_pending=_snapshot())
        clean = getattr(server, method)()
        for snapshot in (clean.openapi_snapshot, clean.openapi_pending):
            assert snapshot.document == {}
            assert snapshot.sha256 == "a" * 64 and snapshot.dialect == "3.1"
        assert server.openapi_snapshot.document  # the original is untouched

    def test_no_snapshot_stays_none(self):
        assert _server(openapi_snapshot=None).sanitize_for_api().openapi_snapshot is None

    async def test_api_key_server_with_no_key_sends_no_stale_bearer(self):
        headers: dict[str, str] = {}
        server = _server(
            auth_type="api_key", api_key_name="k", api_key_in="header", bearer_token="old"
        )
        await apply_server_auth(headers, server, caller=SERVICE_IDENTITY)
        assert headers == {}
