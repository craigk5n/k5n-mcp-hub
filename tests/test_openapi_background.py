"""Story 13.4 (part B) -- the background paths and export for OpenAPI servers.

An OpenAPI server has no MCP endpoint: its `url` is the synthetic `openapi:<id>`.
Discovery computes its tools locally from the mapping instead of probing; health is
skipped unless an admin named a GET probe; export refuses, since there is nothing to
publish.
"""

from __future__ import annotations

import json
import pathlib
import socket
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from mcp_hub.config import HealthCheckConfig, TraceConfig
from mcp_hub.health.checker import HealthChecker
from mcp_hub.mcp.discovery import DiscoveryService
from mcp_hub.mcp.registry_export import to_server_json
from mcp_hub.models import RegisteredServer
from mcp_hub.models.openapi import OpenAPIOperationRef, OpenAPISnapshot
from mcp_hub.registry.service import Registry
from mcp_hub.routes.ui_downloads import resolve_mode_for_server
from mcp_hub.storage.memory import InMemoryStorage
from mcp_hub.trace.recorder import TraceRecorder

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "openapi"


def _server(**fields: Any) -> RegisteredServer:
    document = json.loads((FIXTURES / "petstore-3.1.json").read_text())
    values: dict[str, Any] = {
        "id": "pets",
        "transport_kind": "openapi",
        "url": "",
        "openapi_base_url": "https://petstore.example.com/v1",
        "openapi_snapshot": OpenAPISnapshot(document=document, dialect="3.1", sha256="a" * 64),
        "openapi_selection": [OpenAPIOperationRef(method="GET", path="/pets")],
        "healthy": True,
    }
    values.update(fields)
    return RegisteredServer(**values)


async def _registry(*servers: RegisteredServer) -> Registry:
    storage = InMemoryStorage()
    await storage.init()
    registry = Registry(storage)
    for server in servers:
        await registry.register(server)
    return registry


@pytest.fixture
def no_network(monkeypatch):
    """Fail on any outbound connection attempt, and on any MCP client use."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an OpenAPI server must not be probed over the network")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr("mcp_hub.mcp.discovery.MCPClient", refuse)


# --------------------------------------------------------------------------- discovery


class TestDiscovery:
    async def test_tools_come_from_the_mapping_not_the_network(self, no_network):
        registry = await _registry(_server())
        service = DiscoveryService(registry, openapi_description_limit=1024)
        await service.discover_immediately(await registry.get("pets"))
        stored = await registry.get("pets")
        assert [t["name"] for t in stored.tools] == ["listPets"]
        assert stored.mcp_transport == "openapi"
        assert stored.last_capability_sync is not None

    async def test_poll_includes_on_behalf_of_servers(self, no_network):
        # OBO-only servers are skipped by the poll because capabilities would be
        # discovered as some arbitrary user. OpenAPI tools need no user at all.
        registry = await _registry(_server(auth_type="obo"))
        await DiscoveryService(registry, openapi_description_limit=1024).poll_once()
        assert [t["name"] for t in (await registry.get("pets")).tools] == ["listPets"]

    async def test_an_empty_selection_clears_the_tools(self, no_network):
        registry = await _registry(_server())
        service = DiscoveryService(registry, openapi_description_limit=1024)
        await service.discover_immediately(await registry.get("pets"))
        await registry.register(_server(openapi_selection=[]))
        await service.discover_immediately(await registry.get("pets"))
        assert (await registry.get("pets")).tools == []

    async def test_re_registering_does_not_resurrect_old_tools(self, no_network):
        # Registry.register keeps discovered tools when a re-registration has none.
        # OpenAPI tools are derived from the selection, so that would show tools the
        # admin just removed.
        registry = await _registry(_server(tools=[{"name": "old"}]))
        await registry.register(_server(openapi_selection=[], tools=[]))
        assert (await registry.get("pets")).tools == []

    async def test_a_selection_that_no_longer_maps_is_logged_not_raised(self, no_network, caplog):
        broken = _server(openapi_selection=[OpenAPIOperationRef(method="TRACE", path="/pets")])
        broken.openapi_snapshot.document["paths"]["/pets"]["trace"] = {
            "responses": {"200": {"description": "ok"}}
        }
        registry = await _registry(broken)
        await DiscoveryService(registry, openapi_description_limit=1024).discover_immediately(
            await registry.get("pets")
        )
        assert (await registry.get("pets")).tools == []
        assert "pets" in caplog.text

    async def test_description_limit_applies(self, no_network):
        registry = await _registry(_server())
        await DiscoveryService(registry, openapi_description_limit=5).discover_immediately(
            await registry.get("pets")
        )
        assert len((await registry.get("pets")).tools[0]["description"]) == 5


# --------------------------------------------------------------------------- health


def _checker(registry: Registry, **settings: Any) -> HealthChecker:
    config = HealthCheckConfig(**{"timeout_seconds": 5, **settings})
    return HealthChecker(registry, config, TraceRecorder(), TraceConfig())


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestHealth:
    async def test_skipped_without_a_probe(self):
        server = _server(healthy=True, consecutive_fails=0)
        registry = await _registry(server)
        before = await registry.get("pets")

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("no probe is configured, so nothing may be requested")

        async with _client(handler) as client:
            await _checker(registry)._check_single_server(before, client)
        after = await registry.get("pets")
        assert (after.healthy, after.consecutive_fails, after.last_checked) == (
            before.healthy,
            before.consecutive_fails,
            before.last_checked,
        )

    async def test_probe_2xx_is_healthy(self):
        seen: list[httpx.Request] = []
        server = _server(
            healthy=False,
            openapi_health_probe=OpenAPIOperationRef(method="GET", path="/pets"),
            auth_type="api_key",
            api_key_name="key",
            api_key_in="query",
            api_key_value="s3cret",
        )
        registry = await _registry(server)

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(204)

        async with _client(handler) as client:
            await _checker(registry)._check_single_server(await registry.get("pets"), client)
        assert (await registry.get("pets")).healthy is True
        assert seen[0].method == "GET"
        assert str(seen[0].url) == "https://petstore.example.com/v1/pets?key=s3cret"

    async def test_probe_uses_the_service_identity(self):
        # An OBO server with a service credential: the probe has no user in scope, so
        # it falls through to the service credential (ADR 0004).
        seen: list[httpx.Request] = []
        server = _server(
            auth_type="obo",
            bearer_token="service-token",
            openapi_health_probe=OpenAPIOperationRef(method="GET", path="/pets"),
        )
        registry = await _registry(server)

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200)

        async with _client(handler) as client:
            await _checker(registry)._check_single_server(await registry.get("pets"), client)
        assert seen[0].headers["authorization"] == "Bearer service-token"

    @pytest.mark.parametrize("status", [301, 404, 500, 503])
    async def test_probe_non_2xx_is_unhealthy(self, status):
        server = _server(openapi_health_probe=OpenAPIOperationRef(method="GET", path="/pets"))
        registry = await _registry(server)
        async with _client(lambda r: httpx.Response(status)) as client:
            await _checker(registry)._check_single_server(await registry.get("pets"), client)
        stored = await registry.get("pets")
        assert stored.healthy is False
        assert stored.consecutive_fails == 1

    async def test_probe_429_is_up_but_rate_limited(self):
        # The same reading the HTTP path gives a throttled server.
        server = _server(openapi_health_probe=OpenAPIOperationRef(method="GET", path="/pets"))
        registry = await _registry(server)
        async with _client(lambda r: httpx.Response(429)) as client:
            await _checker(registry)._check_single_server(await registry.get("pets"), client)
        stored = await registry.get("pets")
        assert (stored.healthy, stored.rate_limited) == (True, True)

    async def test_transport_failure_is_unhealthy(self):
        server = _server(openapi_health_probe=OpenAPIOperationRef(method="GET", path="/pets"))
        registry = await _registry(server)

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        async with _client(handler) as client:
            await _checker(registry)._check_single_server(await registry.get("pets"), client)
        assert (await registry.get("pets")).healthy is False

    async def test_threshold_and_auto_unregister_apply(self):
        server = _server(openapi_health_probe=OpenAPIOperationRef(method="GET", path="/pets"))
        registry = await _registry(server)
        checker = _checker(registry, failure_threshold=2, auto_unregister=True)
        async with _client(lambda r: httpx.Response(500)) as client:
            await checker._check_single_server(await registry.get("pets"), client)
            assert await registry.get("pets") is not None
            await checker._check_single_server(await registry.get("pets"), client)
        assert await registry.get("pets") is None


# --------------------------------------------------------------------------- export & downloads


class TestExportAndDownloads:
    def test_export_refuses(self):
        with pytest.raises(ValueError, match="OpenAPI"):
            to_server_json(_server())

    def test_downloads_force_hub_mode(self):
        assert resolve_mode_for_server(_server(), "") == "hub"
        with pytest.raises(HTTPException) as excinfo:
            resolve_mode_for_server(_server(), "direct")
        assert excinfo.value.status_code == 400


class TestReviewFixes:
    """Security review of Story 13.4 (finding 3): discovery must not write a stale or
    deleted record back."""

    async def test_discovery_uses_the_stored_record_not_the_callers_copy(self, no_network):
        registry = await _registry(_server())
        stale = await registry.get("pets")
        await registry.register(
            _server(openapi_selection=[OpenAPIOperationRef(method="POST", path="/pets")])
        )
        await DiscoveryService(registry, openapi_description_limit=1024).discover_immediately(stale)
        stored = await registry.get("pets")
        assert [r.method for r in stored.openapi_selection] == ["POST"]
        assert [t["name"] for t in stored.tools] == ["createPet"]

    async def test_discovery_does_not_resurrect_a_deleted_server(self, no_network):
        registry = await _registry(_server())
        held = await registry.get("pets")
        await registry.unregister("pets")
        await DiscoveryService(registry, openapi_description_limit=1024).discover_immediately(held)
        assert await registry.get("pets") is None
