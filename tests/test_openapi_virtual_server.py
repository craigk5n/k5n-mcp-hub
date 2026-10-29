"""Story 13.6 -- the hub as the MCP server for an OpenAPI backend, end to end.

Driven through the real proxy route (`POST /mcp` with `X-MCP-Target-Server`) on the
real app: the `mcp` SDK client for the handshake revisions, `StatelessMCPClient` for
2026-07-28, and raw JSON-RPC for the edges. The REST API behind the server is an
`httpx.MockTransport` the app is given in place of its default client.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import httpx
import pytest

from mcp_hub.app import create_app
from mcp_hub.auth.caller import SERVICE_IDENTITY
from mcp_hub.config import HealthCheckConfig, SecurityConfig, Settings, StorageConfig
from mcp_hub.mcp.constants import (
    BACKWARD_COMPAT_PROTOCOL_VERSION,
    INVALID_PARAMS,
    INVALID_REQUEST,
    LEGACY_RESOURCE_NOT_FOUND,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    STATELESS_PROTOCOL_VERSION,
    UNSUPPORTED_PROTOCOL_VERSION,
)
from mcp_hub.models import RegisteredServer
from mcp_hub.models.openapi import OpenAPIOperationRef, OpenAPISnapshot
from mcp_hub.openapi import executor, virtual_server

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "openapi"
PETS = [{"id": 1, "name": "rex"}]


def _server(**fields: Any) -> RegisteredServer:
    document = json.loads((FIXTURES / "petstore-3.1.json").read_text())
    values: dict[str, Any] = {
        "id": "pets",
        "url": "",
        "transport_kind": "openapi",
        "openapi_base_url": "https://petstore.example.com/v1",
        "openapi_snapshot": OpenAPISnapshot(document=document, dialect="3.1", sha256="b" * 64),
        "openapi_selection": [
            OpenAPIOperationRef(method="GET", path="/pets"),
            OpenAPIOperationRef(method="POST", path="/pets"),
        ],
    }
    values.update(fields)
    return RegisteredServer(**values)


class Upstream:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        request.read()
        if request.method == "POST":
            return httpx.Response(201, json={"id": 2, **json.loads(request.content)})
        return httpx.Response(200, json=PETS)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def resolve(host: str, *, allow_private_networks: bool = False) -> str | None:
        return "93.184.216.34"

    monkeypatch.setattr(executor, "resolve_pinned_ip", resolve)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "storage": StorageConfig(type="inmemory"),
        "healthcheck": HealthCheckConfig(enabled=False),
        "security": SecurityConfig(allow_private_networks=False),
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
async def hub():
    app = create_app(_settings())
    await app.state.registry.register(_server())
    upstream = Upstream()
    app.state.openapi_http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    yield app, upstream
    await app.state.openapi_http_client.aclose()


async def _rpc(
    app: Any,
    method: str,
    params: Any = None,
    *,
    id: Any = 1,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> httpx.Response:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if id is not None:
        payload["id"] = id
    if params is not None:
        payload["params"] = params
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            "/mcp",
            content=raw if raw is not None else json.dumps(payload).encode(),
            headers={
                "X-MCP-Target-Server": "pets",
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )


# --------------------------------------------------------------------------- protocols


class TestHandshakeRevisionsWithTheSdk:
    async def test_initialize_list_and_call(self, hub):
        import httpx2
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        app, upstream = hub
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-MCP-Target-Server": "pets"},
        ) as http:
            async with streamable_http_client("http://test/mcp", http_client=http) as (read, write):
                async with ClientSession(read_stream=read, write_stream=write) as session:
                    init = await session.initialize()
                    assert init.server_info.name == "pets"
                    tools = await session.list_tools()
                    assert {t.name for t in tools.tools} == {"listPets", "createPet"}
                    result = await session.call_tool("listPets", {"limit": 5})
        assert result.is_error is False
        assert result.structured_content == {"result": PETS}
        assert str(upstream.requests[0].url) == "https://petstore.example.com/v1/pets?limit=5"


class TestStatelessRevision:
    async def test_discover_list_and_call(self, hub, monkeypatch):
        import mcp_hub.mcp.stateless as stateless

        app, upstream = hub

        def asgi_client(**kwargs: Any) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), headers={"X-MCP-Target-Server": "pets"}
            )

        monkeypatch.setattr(stateless, "safe_http_client_factory", asgi_client)
        client = stateless.StatelessMCPClient("http://test/mcp", caller=SERVICE_IDENTITY)
        discovered = await client.discover()
        assert discovered.protocol_version == STATELESS_PROTOCOL_VERSION
        tools = await client.list("tools/list")
        assert {t["name"] for t in tools["tools"]} == {"listPets", "createPet"}

    async def test_stateless_results_carry_the_required_fields(self, hub):
        app, _ = hub
        response = await _rpc(
            app, "tools/list", {}, headers={"MCP-Protocol-Version": STATELESS_PROTOCOL_VERSION}
        )
        result = response.json()["result"]
        assert result["resultType"] == "complete"
        assert result["ttlMs"] > 0 and result["cacheScope"] == "private"
        call = await _rpc(
            app,
            "tools/call",
            {"name": "listPets", "arguments": {}},
            headers={"MCP-Protocol-Version": STATELESS_PROTOCOL_VERSION},
        )
        assert call.json()["result"]["resultType"] == "complete"

    async def test_initialize_is_not_a_stateless_method(self, hub):
        app, _ = hub
        response = await _rpc(
            app, "initialize", {}, headers={"MCP-Protocol-Version": STATELESS_PROTOCOL_VERSION}
        )
        assert response.json()["error"]["code"] == METHOD_NOT_FOUND

    async def test_an_unsupported_meta_version_is_refused(self, hub):
        app, _ = hub
        params = {"_meta": {"io.modelcontextprotocol/protocolVersion": "1999-01-01"}}
        response = await _rpc(app, "tools/list", params)
        assert response.json()["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION


class TestInitialize:
    @pytest.mark.parametrize(
        "requested, negotiated",
        [
            (PROTOCOL_VERSION, PROTOCOL_VERSION),
            (BACKWARD_COMPAT_PROTOCOL_VERSION, BACKWARD_COMPAT_PROTOCOL_VERSION),
            ("1999-01-01", PROTOCOL_VERSION),
            (None, PROTOCOL_VERSION),
        ],
    )
    async def test_version_negotiation(self, hub, requested, negotiated):
        app, _ = hub
        params = {"capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
        if requested:
            params["protocolVersion"] = requested
        result = (await _rpc(app, "initialize", params)).json()["result"]
        assert result["protocolVersion"] == negotiated
        assert set(result["capabilities"]) == {"tools", "resources"}

    async def test_notifications_get_202(self, hub):
        app, _ = hub
        response = await _rpc(app, "notifications/initialized", id=None)
        assert response.status_code == 202 and response.content == b""

    async def test_ping(self, hub):
        app, _ = hub
        assert (await _rpc(app, "ping")).json()["result"] == {}


class TestStrictParsing:
    @pytest.mark.parametrize(
        "raw, status, code",
        [
            (b"{nope", 400, PARSE_ERROR),
            (
                b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "method": "tools/call"}',
                400,
                PARSE_ERROR,
            ),
            (b'[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]', 400, INVALID_REQUEST),
            (b'"ping"', 400, INVALID_REQUEST),
            (b'{"jsonrpc": "2.0", "id": 1}', 400, INVALID_REQUEST),
            (b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]}', 200, INVALID_PARAMS),
        ],
        ids=["bad-json", "duplicate-key", "batch", "not-an-object", "no-method", "array-params"],
    )
    async def test_refused(self, hub, raw, status, code):
        app, _ = hub
        response = await _rpc(app, "ignored", raw=raw)
        assert response.status_code == status
        assert response.json()["error"]["code"] == code

    @pytest.mark.parametrize(
        "method", ["prompts/list", "completion/complete", "tools/delete", "logging/setLevel"]
    )
    async def test_anything_else_is_method_not_found(self, hub, method):
        app, _ = hub
        assert (await _rpc(app, method, {})).json()["error"]["code"] == METHOD_NOT_FOUND


# --------------------------------------------------------------------------- tools


class TestTools:
    async def test_list_has_only_the_selection(self, hub):
        app, _ = hub
        tools = (await _rpc(app, "tools/list", {})).json()["result"]["tools"]
        assert {t["name"] for t in tools} == {"listPets", "createPet"}

    async def test_call(self, hub):
        app, upstream = hub
        result = (
            await _rpc(app, "tools/call", {"name": "createPet", "arguments": {"name": "fido"}})
        ).json()["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["name"] == "fido"
        assert json.loads(upstream.requests[0].content) == {"name": "fido"}

    @pytest.mark.parametrize("name", ["showPetById", "nope"])
    async def test_unselected_and_unknown_look_the_same(self, hub, name):
        # A hidden tool must not confirm it exists (ADR 0010).
        app, upstream = hub
        error = (await _rpc(app, "tools/call", {"name": name, "arguments": {}})).json()["error"]
        assert error == {"code": INVALID_PARAMS, "message": f"Unknown tool: {name}"}
        assert upstream.requests == []

    async def test_invalid_arguments_are_a_tool_error_not_a_protocol_error(self, hub):
        app, upstream = hub
        result = (await _rpc(app, "tools/call", {"name": "createPet", "arguments": {}})).json()[
            "result"
        ]
        assert result["isError"] is True
        assert upstream.requests == []

    async def test_malformed_call_params(self, hub):
        app, _ = hub
        error = (await _rpc(app, "tools/call", {"name": 5})).json()["error"]
        assert error["code"] == INVALID_PARAMS

    async def test_every_call_goes_through_the_one_seam(self, hub, monkeypatch):
        # Epic 12's tool policy wraps dispatch_tool_call; nothing may bypass it.
        app, _ = hub
        seen: list[str] = []
        real = virtual_server.dispatch_tool_call

        async def spy(name: str, arguments: dict[str, Any], ctx: Any) -> Any:
            seen.append(name)
            return await real(name, arguments, ctx)

        monkeypatch.setattr(virtual_server, "dispatch_tool_call", spy)
        await _rpc(app, "tools/call", {"name": "listPets", "arguments": {}})
        await _rpc(app, "tools/call", {"name": "nope", "arguments": {}})
        assert seen == ["listPets", "nope"]

    async def test_on_behalf_of_needing_a_user_is_401(self, hub):
        app, upstream = hub
        await app.state.registry.register(
            _server(
                auth_type="obo",
                oauth_token_url="https://idp.example.com/token",
                oauth_client_id="h",
                oauth_client_secret="s",
            )
        )
        response = await _rpc(app, "tools/call", {"name": "listPets", "arguments": {}})
        assert response.status_code == 401
        assert upstream.requests == []


# --------------------------------------------------------------------------- resources


class TestResources:
    async def test_list(self, hub):
        app, _ = hub
        resources = (await _rpc(app, "resources/list", {})).json()["result"]["resources"]
        assert [r["uri"] for r in resources] == [virtual_server.spec_uri(_server())]

    async def test_read_is_trimmed_to_the_selection(self, hub):
        app, _ = hub
        uri = virtual_server.spec_uri(_server())
        content = (await _rpc(app, "resources/read", {"uri": uri})).json()["result"]["contents"][0]
        assert content["mimeType"] == "application/json"
        assert "/pets/{petId}" not in content["text"]
        assert "showPetById" not in content["text"]
        assert set(json.loads(content["text"])["paths"]) == {"/pets"}

    @pytest.mark.parametrize(
        "stateless, code", [(False, LEGACY_RESOURCE_NOT_FOUND), (True, INVALID_PARAMS)]
    )
    async def test_unknown_resource(self, hub, stateless, code):
        app, _ = hub
        headers = {"MCP-Protocol-Version": STATELESS_PROTOCOL_VERSION} if stateless else {}
        error = (
            await _rpc(app, "resources/read", {"uri": "openapi://other/spec"}, headers=headers)
        ).json()["error"]
        assert error["code"] == code


# --------------------------------------------------------------------------- the shared proxy path


class TestSharedProxyPath:
    async def test_required_scope_is_enforced(self):
        from tests.test_multitenant_integration import (
            AUDIENCE,
            ISSUER,
            JWKS_URI,
            jwks_client,
            token_for,
        )
        from mcp_hub.config import AuthConfig, JWTAuthConfig

        app = create_app(
            _settings(
                auth=AuthConfig(
                    type="jwt",
                    jwt=JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, jwks_uri=JWKS_URI),
                )
            )
        )
        app.state.authenticator._client = jwks_client()  # type: ignore[attr-defined]
        await app.state.registry.register(_server(required_scope="pets:use"))
        app.state.openapi_http_client = httpx.AsyncClient(transport=httpx.MockTransport(Upstream()))
        denied = await _rpc(
            app,
            "tools/list",
            {},
            headers={"Authorization": f"Bearer {token_for('bob', 'other:use')}"},
        )
        allowed = await _rpc(
            app,
            "tools/list",
            {},
            headers={"Authorization": f"Bearer {token_for('alice', 'pets:use')}"},
        )
        assert denied.status_code == 403
        assert allowed.status_code == 200

    async def test_fault_injection_applies(self, hub):
        app, upstream = hub
        server = _server()
        server.fault_injection.enabled = True
        server.fault_injection.malformed_json = True
        await app.state.registry.register(server)
        response = await _rpc(app, "tools/call", {"name": "listPets", "arguments": {}})
        with pytest.raises(ValueError):
            json.loads(response.content)
        assert upstream.requests == []

    async def test_traces_cover_the_exchange_and_the_upstream_request_sanitized(self, hub):
        app, _ = hub
        await app.state.registry.register(
            _server(
                trace_verbose=True,
                auth_type="api_key",
                api_key_name="key",
                api_key_in="query",
                api_key_value="s3cret",
            )
        )
        await _rpc(app, "tools/call", {"name": "listPets", "arguments": {}})
        entries = app.state.trace_recorder.list("pets")
        operations = [e.operation for e in entries]
        assert "proxy" in operations and "openapi_call" in operations
        assert "s3cret" not in json.dumps([e.__dict__ for e in entries], default=str)
        upstream_entry = next(e for e in entries if e.operation == "openapi_call")
        assert upstream_entry.outbound_url.startswith("https://petstore.example.com/v1/pets")

    async def test_header_api_key_is_redacted_in_traces(self, hub):
        app, _ = hub
        await app.state.registry.register(
            _server(
                trace_verbose=True,
                auth_type="api_key",
                api_key_name="X-Pet-Key",
                api_key_in="header",
                api_key_value="s3cret",
            )
        )
        await _rpc(app, "tools/call", {"name": "listPets", "arguments": {}})
        entries = app.state.trace_recorder.list("pets")
        assert "s3cret" not in json.dumps([e.__dict__ for e in entries], default=str)

    async def test_prometheus_metrics_count_it(self, hub):
        app, _ = hub
        await _rpc(app, "tools/list", {})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            metrics = (await client.get("/metrics")).text
        assert "requests_total" in metrics


class TestTelemetry:
    async def test_span_and_metric_use_the_path_template(self, hub):
        pytest.importorskip("opentelemetry.sdk")
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        from tests.test_otel_spans import _recording

        app, _ = hub
        await app.state.registry.register(
            _server(openapi_selection=[OpenAPIOperationRef(method="GET", path="/pets/{petId}")])
        )
        exporter = _recording(app)
        reader = InMemoryMetricReader()
        app.state.otel._meter = MeterProvider(metric_readers=[reader]).get_meter("test")  # type: ignore[attr-defined]
        await _rpc(
            app, "tools/call", {"name": "showPetById", "arguments": {"petId": "secret-pet-123"}}
        )

        (span,) = [s for s in exporter.get_finished_spans() if s.name == "mcp.proxy"]
        assert span.attributes["http.route"] == "GET /pets/{petId}"
        assert span.attributes["mcp.transport"] == "openapi"
        assert span.attributes["mcp.tool.name"] == "showPetById"
        assert "secret-pet-123" not in json.dumps(dict(span.attributes))
        data = reader.get_metrics_data().to_json()
        assert "GET /pets/{petId}" in data and "secret-pet-123" not in data


class TestToolCache:
    def test_reused_until_the_spec_or_choices_change_and_bounded(self, monkeypatch):
        import mcp_hub.openapi.server_tools as server_tools

        monkeypatch.setattr(server_tools, "_cache", server_tools.OrderedDict())
        calls: list[int] = []
        real = server_tools.openapi_tools

        def counting(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            return real(*args, **kwargs)

        monkeypatch.setattr(server_tools, "openapi_tools", counting)
        server = _server()
        server_tools.cached_openapi_tools(server, description_limit=1024)
        server_tools.cached_openapi_tools(server, description_limit=1024)
        assert len(calls) == 1
        changed = _server(openapi_selection=[OpenAPIOperationRef(method="GET", path="/pets")])
        assert [
            t.definition.name
            for t in server_tools.cached_openapi_tools(changed, description_limit=1024)
        ] == ["listPets"]
        assert len(calls) == 2

        monkeypatch.setattr(server_tools, "_CACHE_SIZE", 2)
        for i in range(4):
            snap = server.openapi_snapshot.model_copy(update={"sha256": f"{i}" * 64})
            server_tools.cached_openapi_tools(
                _server(openapi_snapshot=snap), description_limit=1024
            )
        assert len(server_tools._cache) == 2


class TestAuthenticationChallenge:
    async def test_401_under_jwt_carries_the_challenge(self):
        from tests.test_multitenant_integration import (
            AUDIENCE,
            ISSUER,
            JWKS_URI,
            jwks_client,
            token_for,
        )
        from mcp_hub.config import AuthConfig, JWTAuthConfig

        app = create_app(
            _settings(
                auth=AuthConfig(
                    type="jwt",
                    jwt=JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, jwks_uri=JWKS_URI),
                )
            )
        )
        app.state.authenticator._client = jwks_client()  # type: ignore[attr-defined]
        # OBO with no token endpoint the hub can use for this caller: the exchange
        # needs authentication the caller's token cannot provide.
        await app.state.registry.register(
            _server(
                auth_type="obo",
                required_scope="pets:use",
                oauth_token_url="https://idp.example.com/token",
                oauth_client_id="h",
                oauth_client_secret="s",
            )
        )

        async def needs_auth(*args: Any, **kwargs: Any) -> None:
            from mcp_hub.mcp.auth import OBOAuthError

            raise OBOAuthError("no user token", needs_authentication=True)

        import mcp_hub.openapi.executor as executor_module

        original = executor_module.call_tool.__kwdefaults__["apply_auth"]
        executor_module.call_tool.__kwdefaults__["apply_auth"] = needs_auth
        try:
            response = await _rpc(
                app,
                "tools/call",
                {"name": "listPets", "arguments": {}},
                headers={"Authorization": f"Bearer {token_for('alice', 'pets:use')}"},
            )
        finally:
            executor_module.call_tool.__kwdefaults__["apply_auth"] = original
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")


class TestReviewFixes:
    """Security review of Story 13.6, findings 2, 3, 4, 5, 6 and 7."""

    @pytest.mark.parametrize("version", [[1], {}, 5, True])
    async def test_a_non_string_meta_version_is_refused_not_a_500(self, hub, version):
        app, _ = hub
        response = await _rpc(
            app, "ping", {"_meta": {"io.modelcontextprotocol/protocolVersion": version}}
        )
        assert response.status_code == 200
        assert response.json()["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION

    async def test_an_unexpected_failure_is_an_internal_error_not_a_500(self, hub, monkeypatch):
        app, _ = hub

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("internal detail")

        monkeypatch.setitem(virtual_server._HANDLERS, "ping", boom)
        response = await _rpc(app, "ping", {})
        assert response.status_code == 200
        assert response.json()["error"]["code"] == -32603
        assert "internal detail" not in response.text

    @pytest.mark.parametrize(
        "raw",
        [
            b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": NaN}}',
            b'{"jsonrpc": "2.0", "id": 1e999, "method": "ping"}',
        ],
        ids=["nan", "infinite-id"],
    )
    async def test_non_finite_numbers_are_a_parse_error(self, hub, raw):
        app, _ = hub
        response = await _rpc(app, "ignored", raw=raw)
        assert response.json()["error"]["code"] == PARSE_ERROR

    @pytest.mark.parametrize("request_id", [{"a": 1}, [1], True])
    async def test_id_must_be_a_string_number_or_null(self, hub, request_id):
        app, _ = hub
        response = await _rpc(app, "ping", {}, id=request_id)
        assert response.json()["error"]["code"] == INVALID_REQUEST
        assert response.json()["id"] is None

    @pytest.mark.parametrize("params", [0, "", False])
    async def test_falsy_non_object_params_are_invalid(self, hub, params):
        app, _ = hub
        assert (await _rpc(app, "ping", params)).json()["error"]["code"] == INVALID_PARAMS

    async def test_untraced_argument_values_stay_out_of_traces(self, hub):
        app, _ = hub
        await app.state.registry.register(
            _server(openapi_selection=[OpenAPIOperationRef(method="GET", path="/pets/{petId}")])
        )
        await _rpc(
            app, "tools/call", {"name": "showPetById", "arguments": {"petId": "alice@example.com"}}
        )
        entry = next(
            e for e in app.state.trace_recorder.list("pets") if e.operation == "openapi_call"
        )
        assert entry.outbound_url == "https://petstore.example.com/v1/pets/{petId}"

    async def test_verbose_traces_keep_the_expanded_url(self, hub):
        app, _ = hub
        await app.state.registry.register(
            _server(
                trace_verbose=True,
                openapi_selection=[OpenAPIOperationRef(method="GET", path="/pets/{petId}")],
            )
        )
        await _rpc(app, "tools/call", {"name": "showPetById", "arguments": {"petId": "7"}})
        entry = next(
            e for e in app.state.trace_recorder.list("pets") if e.operation == "openapi_call"
        )
        assert entry.outbound_url == "https://petstore.example.com/v1/pets/7"

    async def test_resources_read_is_cached(self, hub, monkeypatch):
        app, _ = hub
        calls: list[int] = []
        real = virtual_server.trimmed_spec

        def counting(*args: Any) -> Any:
            calls.append(1)
            return real(*args)

        monkeypatch.setattr(virtual_server, "trimmed_spec", counting)
        monkeypatch.setattr(virtual_server, "_spec_cache", virtual_server.OrderedDict())
        uri = virtual_server.spec_uri(_server())
        first = await _rpc(app, "resources/read", {"uri": uri})
        second = await _rpc(app, "resources/read", {"uri": uri})
        assert first.json()["result"] == second.json()["result"]
        assert len(calls) == 1

    def test_mapping_failures_are_cached_too(self, monkeypatch):
        import mcp_hub.openapi.server_tools as server_tools
        from mcp_hub.openapi.model import MappingError

        monkeypatch.setattr(server_tools, "_cache", server_tools.OrderedDict())
        calls: list[int] = []

        def failing(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            raise MappingError(["nope"])

        monkeypatch.setattr(server_tools, "openapi_tools", failing)
        for _ in range(3):
            with pytest.raises(MappingError):
                server_tools.cached_openapi_tools(_server(), description_limit=1024)
        assert len(calls) == 1

    async def test_server_version_from_the_spec_is_bounded(self, hub):
        app, _ = hub
        server = _server()
        server.openapi_snapshot.document["info"]["version"] = "v" * 10_000
        await app.state.registry.register(server)
        result = (await _rpc(app, "initialize", {})).json()["result"]
        assert len(result["serverInfo"]["version"]) <= 64

    async def test_get_is_405(self, hub):
        app, _ = hub
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.request(
                "GET",
                "/mcp/session",
                headers={"X-MCP-Target-Server": "pets"},
                content=b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"listPets"}}',
            )
        assert response.status_code == 405
