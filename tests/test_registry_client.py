"""Client for the official MCP registry (Epic 10, Story 10.1).

Tested against a page recorded from the live index rather than a hand-written
payload, because the interesting properties of this API are the ones a fixture
invented from the docs would not have: records carry differing `$schema` values,
the same server appears once per version unless filtered, and pagination is a
cursor in `metadata.nextCursor` rather than an offset.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mcp_hub.mcp.registry_client import (
    DEFAULT_REGISTRY_BASE_URL,
    RegistryClient,
    RegistryRecord,
    parse_registry_page,
)

PAGE = json.loads((Path(__file__).parent / "fixtures" / "registry_page.json").read_text())


class TestParsing:
    def test_parses_a_recorded_page(self) -> None:
        records, cursor = parse_registry_page(PAGE)
        assert records, "the recorded page has servers in it"
        assert all(isinstance(r, RegistryRecord) for r in records)
        assert cursor == PAGE["metadata"].get("nextCursor")

    def test_carries_the_fields_an_import_needs(self) -> None:
        records, _ = parse_registry_page(PAGE)
        first = records[0]
        assert first.name
        assert first.version
        # Every record is either remote-addressable or a package; both are represented.
        assert first.remotes or first.packages

    def test_unknown_fields_and_schema_versions_are_tolerated(self) -> None:
        """Records in the live index carry several `$schema` values, and the schema
        keeps moving. Ignoring what we do not recognise is the only stable posture."""
        page = {
            "servers": [
                {
                    "server": {
                        "$schema": "https://example.invalid/schemas/2099-01-01/server.schema.json",
                        "name": "com.example/thing",
                        "description": "d",
                        "version": "1.0.0",
                        "remotes": [
                            {"type": "streamable-http", "url": "https://e.example.com/mcp"}
                        ],
                        "someFutureField": {"nested": True},
                    },
                    "_meta": {"io.modelcontextprotocol.registry/official": {"isLatest": True}},
                }
            ],
            "metadata": {},
        }
        records, cursor = parse_registry_page(page)
        assert len(records) == 1
        assert records[0].remotes[0]["url"] == "https://e.example.com/mcp"
        assert cursor is None

    def test_a_broken_record_is_skipped_not_fatal(self) -> None:
        # One bad row must not cost the whole page -- the same leniency the capability
        # parser applies to a non-conformant server.
        page = {
            "servers": [
                {"server": {"name": "com.example/ok", "version": "1", "remotes": []}},
                {"server": "not-an-object"},
                {"nothing": "useful"},
            ],
            "metadata": {},
        }
        records, _ = parse_registry_page(page)
        assert [r.name for r in records] == ["com.example/ok"]


class TestClient:
    @pytest.mark.asyncio
    async def test_search_requests_latest_versions_only(self) -> None:
        """Without `version=latest` the list returns one row per version, so the same
        server appears over and over -- useless in a picker."""
        seen: dict[str, Any] = {}

        class _Response:
            status_code = 200

            def json(self) -> Any:
                return PAGE

            def raise_for_status(self) -> None:
                return None

        class _Client:
            async def get(self, url: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
                seen["url"] = url
                seen["params"] = params or {}
                return _Response()

        client = RegistryClient(http_client=_Client())
        records = await client.search("github", limit=3)

        assert records
        assert seen["params"].get("version") == "latest"
        assert seen["params"].get("search") == "github"
        assert seen["url"].startswith(DEFAULT_REGISTRY_BASE_URL)
        assert "/v0.1/servers" in seen["url"]

    @pytest.mark.asyncio
    async def test_pagination_is_bounded(self) -> None:
        """A registry that always returns a cursor must cost a bounded number of round
        trips, exactly as `pagination.collect_pages` bounds capability listing."""
        calls = {"n": 0}

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> Any:
                calls["n"] += 1
                return {
                    "servers": [{"server": {"name": f"com.example/s{calls['n']}", "version": "1"}}],
                    "metadata": {"nextCursor": "always-more"},
                }

        class _Client:
            async def get(self, url: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
                return _Response()

        client = RegistryClient(http_client=_Client())
        records = await client.list_all(max_pages=5)

        assert calls["n"] == 5, "must stop at max_pages rather than follow forever"
        assert len(records) == 5

    @pytest.mark.asyncio
    async def test_a_configured_base_url_is_used(self) -> None:
        seen: dict[str, Any] = {}

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> Any:
                return {"servers": [], "metadata": {}}

        class _Client:
            async def get(self, url: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
                seen["url"] = url
                return _Response()

        client = RegistryClient(
            base_url="https://registry.internal.example/", http_client=_Client()
        )
        await client.search("x")

        assert seen["url"] == "https://registry.internal.example/v0.1/servers"
