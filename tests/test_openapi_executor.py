"""Story 13.5 -- turning a tools/call into the REST request, and its response into a
result (ADR 0011 §6-§8).

Every request goes through an `httpx.MockTransport`, whose handler records what the
upstream would have received. A test that expects *no* request passes a handler
that fails if called. `resolve_pinned_ip` is stubbed to a public address, because
the executor re-resolves each hop explicitly before sending.
"""

from __future__ import annotations

import base64
import functools
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from mcp_hub.auth.caller import SERVICE_IDENTITY
from mcp_hub.auth.principal import Principal
from mcp_hub.mcp.auth import apply_server_auth
from mcp_hub.mcp.obo_cache import OBOTokenCache
from mcp_hub.mcp.token_exchange import ExchangedToken, TokenExchangeError
from mcp_hub.models import RegisteredServer
from mcp_hub.openapi import executor
from mcp_hub.openapi.executor import CallLimits, call_tool
from mcp_hub.openapi.mapping import build_tools
from mcp_hub.openapi.model import OperationKey, Tool

BASE = "https://api.example.com/v1"
LIMITS = CallLimits(
    timeout_seconds=5, max_response_bytes=1_000_000, max_redirects=3, allow_private_networks=False
)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every host resolves to a public address unless a test says otherwise."""

    async def resolve(host: str, *, allow_private_networks: bool = False) -> str | None:
        return "93.184.216.34"

    monkeypatch.setattr(executor, "resolve_pinned_ip", resolve)


def _spec(paths: dict[str, Any], components: dict[str, Any] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": paths,
    }
    if components:
        doc["components"] = components
    return doc


def _tool(paths: dict[str, Any], method: str, path: str, components=None) -> Tool:
    doc = _spec(paths, components)
    (tool,) = build_tools(
        doc,
        "3.1",
        selection=frozenset({OperationKey(method, path)}),
        overrides={},
        description_limit=1024,
    )
    return tool


def _server(**fields: Any) -> RegisteredServer:
    values: dict[str, Any] = {
        "id": "api",
        "transport_kind": "openapi",
        "url": "",
        "openapi_base_url": BASE,
    }
    values.update(fields)
    return RegisteredServer(**values)


def _op(**fields: Any) -> dict[str, Any]:
    return {"responses": {"200": {"description": "ok"}}, **fields}


def _param(name: str, location: str = "query", **fields: Any) -> dict[str, Any]:
    return {"name": name, "in": location, "schema": {"type": "string"}, **fields}


class Upstream:
    """A mock upstream that records requests and answers from a list."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses) or [httpx.Response(200, json={})]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        request.read()
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def _never(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError(f"no request expected, got {request.method} {request.url}")


async def _call(tool: Tool, arguments: dict[str, Any], upstream: Any = None, **kwargs: Any):
    handler = upstream if upstream is not None else Upstream()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        kwargs.setdefault("server", _server())
        kwargs.setdefault("caller", SERVICE_IDENTITY)
        kwargs.setdefault("limits", LIMITS)
        return await call_tool(tool, arguments, http_client=client, **kwargs)


def _text(result) -> str:
    return "\n".join(c["text"] for c in result.content if c["type"] == "text")


# --------------------------------------------------------------------------- validation


ITEM_PATH = "/things/{id}"


def _item_tool(**schema: Any) -> Tool:
    param = _param("id", "path", required=True, schema={"type": "string", **schema})
    return _tool({ITEM_PATH: {"get": _op(parameters=[param])}}, "GET", ITEM_PATH)


class TestValidation:
    def test_the_engine_is_re2(self):
        # Fails on an assertion, not a hang, if `re` replaces RE2: Python's engine
        # compiles a backreference, and RE2 refuses one.
        from mcp_hub.openapi.validation import _compile

        assert _compile(r"(a)\1") is None
        assert _compile(r"^[a-z]+$") is not None

    async def test_missing_required_argument(self):
        result = await _call(_item_tool(), {}, _never)
        assert result.is_error
        assert "id" in _text(result)

    async def test_wrong_type(self):
        tool = _tool(
            {"/t": {"get": _op(parameters=[_param("n", schema={"type": "integer"})])}}, "GET", "/t"
        )
        result = await _call(tool, {"n": "five"}, _never)
        assert result.is_error and "n" in _text(result)

    async def test_unknown_argument(self):
        result = await _call(_item_tool(), {"id": "1", "extra": 2}, _never)
        assert result.is_error and "extra" in _text(result)

    async def test_catastrophic_pattern_is_checked_in_linear_time(self):
        # `(a+)+$` against a long run of a's and a mismatch is exponential in a
        # backtracking engine; this would hang the hub holding the GIL.
        start = time.monotonic()
        result = await _call(_item_tool(pattern="^(a+)+$"), {"id": "a" * 64 + "!"}, _never)
        assert time.monotonic() - start < 1
        assert result.is_error and "pattern" in _text(result).lower()

    async def test_matching_pattern_passes(self):
        upstream = Upstream()
        result = await _call(_item_tool(pattern="^[a-z]+$"), {"id": "abc"}, upstream)
        assert not result.is_error and len(upstream.requests) == 1

    async def test_pattern_properties_use_the_same_engine(self):
        body = {"type": "object", "patternProperties": {"^(a+)+$": {"type": "integer"}}}
        tool = _tool(
            {"/t": {"post": _op(requestBody={"content": {"application/json": {"schema": body}}})}},
            "POST",
            "/t",
        )
        start = time.monotonic()
        result = await _call(tool, {"body": {"a" * 64 + "!": "x"}})
        assert time.monotonic() - start < 1
        assert not result.is_error  # no key matches, so the value is unconstrained

    async def test_a_pattern_re2_cannot_compile_is_left_to_the_upstream(self):
        upstream = Upstream()
        result = await _call(_item_tool(pattern=r"^(a)\1$"), {"id": "zz"}, upstream)
        assert not result.is_error and len(upstream.requests) == 1


# --------------------------------------------------------------------------- path


class TestPath:
    @pytest.mark.parametrize(
        "value, raw_path",
        [
            ("../admin", "/v1/things/..%2Fadmin"),
            ("..", "/v1/things/%2E%2E"),
            (".", "/v1/things/%2E"),
            ("a/b", "/v1/things/a%2Fb"),
            ("%2e%2e", "/v1/things/%252e%252e"),
            ("a b?c#d", "/v1/things/a%20b%3Fc%23d"),
            ("é", "/v1/things/%C3%A9"),
        ],
    )
    async def test_values_are_encoded_as_one_segment(self, value, raw_path):
        upstream = Upstream()
        result = await _call(_item_tool(), {"id": value}, upstream)
        assert not result.is_error, _text(result)
        assert upstream.requests[0].url.raw_path.decode() == raw_path

    async def test_empty_required_path_value_is_refused(self):
        result = await _call(_item_tool(), {"id": ""}, _never)
        assert result.is_error and "id" in _text(result)

    @pytest.mark.parametrize("value, segment", [(5, "5"), (True, "true"), (1.5, "1.5")])
    async def test_scalars(self, value, segment):
        param = _param(
            "id", "path", required=True, schema={"type": ["integer", "number", "boolean"]}
        )
        tool = _tool({ITEM_PATH: {"get": _op(parameters=[param])}}, "GET", ITEM_PATH)
        upstream = Upstream()
        await _call(tool, {"id": value}, upstream)
        assert upstream.requests[0].url.raw_path.decode() == f"/v1/things/{segment}"

    async def test_base_path_is_kept(self):
        upstream = Upstream()
        await _call(
            _item_tool(),
            {"id": "1"},
            upstream,
            server=_server(openapi_base_url="https://api.example.com/base/"),
        )
        assert upstream.requests[0].url.raw_path.decode() == "/base/things/1"


# --------------------------------------------------------------------------- query & headers


def _query_tool(*params: dict[str, Any]) -> Tool:
    return _tool({"/t": {"get": _op(parameters=list(params))}}, "GET", "/t")


ARRAY = {"type": "array", "items": {"type": "string"}}


class TestQuery:
    @pytest.mark.parametrize(
        "param, args, query",
        [
            (_param("q"), {"q": "a b"}, "q=a+b"),
            (_param("n", schema={"type": "integer"}), {"n": 3}, "n=3"),
            (_param("f", schema={"type": "boolean"}), {"f": False}, "f=false"),
            (_param("tags", schema=ARRAY), {"tags": ["a", "b"]}, "tags=a&tags=b"),
            (_param("tags", schema=ARRAY, explode=False), {"tags": ["a", "b"]}, "tags=a%2Cb"),
            (_param("tags", schema=ARRAY), {"tags": []}, ""),
            (_param("q"), {}, ""),
        ],
    )
    async def test_form_style(self, param, args, query):
        upstream = Upstream()
        result = await _call(_query_tool(param), args, upstream)
        assert not result.is_error, _text(result)
        assert upstream.requests[0].url.query.decode() == query


class TestHeaders:
    async def test_header_parameter_is_sent(self):
        upstream = Upstream()
        await _call(_query_tool(_param("X-Trace", "header")), {"X-Trace": "abc"}, upstream)
        assert upstream.requests[0].headers["x-trace"] == "abc"

    @pytest.mark.parametrize("value", ["a\r\nX-Injected: 1", "a\nb", "a\x00b"])
    async def test_control_characters_are_refused(self, value):
        result = await _call(_query_tool(_param("X-Trace", "header")), {"X-Trace": value}, _never)
        assert result.is_error and "X-Trace" in _text(result)

    async def test_only_the_hubs_own_headers_reach_the_upstream(self):
        # The executor is handed no inbound request at all, so nothing of the
        # caller's (cookies, their Authorization, X-Forwarded-*) can leak through.
        upstream = Upstream()
        await _call(
            _query_tool(_param("X-Trace", "header")),
            {"X-Trace": "t"},
            upstream,
            server=_server(auth_type="bearer", bearer_token="tok"),
        )
        assert set(upstream.requests[0].headers) <= {
            "host",
            "accept",
            "accept-encoding",
            "connection",
            "user-agent",
            "x-trace",
            "authorization",
        }
        assert upstream.requests[0].headers["authorization"] == "Bearer tok"


# --------------------------------------------------------------------------- bodies


OBJECT = {
    "type": "object",
    "required": ["name"],
    "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "tags": ARRAY},
}


def _body_tool(
    schema: Any, media: str = "application/json", required: bool = True, params=()
) -> Tool:
    op = _op(
        parameters=list(params),
        requestBody={"required": required, "content": {media: {"schema": schema}}},
    )
    path = "/things/{id}" if params else "/things"
    return _tool({path: {"post": op}}, "POST", path)


class TestBodies:
    async def test_merged_json_body_holds_only_what_was_sent(self):
        upstream = Upstream()
        await _call(_body_tool(OBJECT), {"name": "rex"}, upstream)
        request = upstream.requests[0]
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {"name": "rex"}

    async def test_renamed_property_goes_back_to_its_own_name(self):
        upstream = Upstream()
        tool = _body_tool(OBJECT, params=[_param("id", "path", required=True)])
        await _call(tool, {"id": "7", "body_id": 42, "name": "rex"}, upstream)
        assert upstream.requests[0].url.raw_path.decode() == "/v1/things/7"
        assert json.loads(upstream.requests[0].content) == {"id": 42, "name": "rex"}

    async def test_whole_body(self):
        upstream = Upstream()
        await _call(_body_tool(ARRAY), {"body": ["a", "b"]}, upstream)
        assert json.loads(upstream.requests[0].content) == ["a", "b"]

    async def test_optional_body_with_nothing_sent_is_omitted(self):
        upstream = Upstream()
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        await _call(_body_tool(schema, required=False), {}, upstream)
        assert upstream.requests[0].content == b""
        assert "content-type" not in upstream.requests[0].headers

    async def test_vendor_json_media_type_is_used_as_declared(self):
        upstream = Upstream()
        await _call(_body_tool(OBJECT, media="application/vnd.api+json"), {"name": "x"}, upstream)
        assert upstream.requests[0].headers["content-type"] == "application/vnd.api+json"

    async def test_form_body(self):
        upstream = Upstream()
        await _call(
            _body_tool(OBJECT, media="application/x-www-form-urlencoded"),
            {"name": "a b", "tags": ["x", "y"]},
            upstream,
        )
        request = upstream.requests[0]
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert request.content == b"name=a+b&tags=x&tags=y"

    async def test_content_type_cannot_come_from_arguments(self):
        # Mapping drops a declared Content-Type header parameter; the hub's own
        # value is the only one sent.
        op = _op(
            parameters=[_param("Content-Type", "header")],
            requestBody={"content": {"application/json": {"schema": OBJECT}}},
        )
        tool = _tool({"/t": {"post": op}}, "POST", "/t")
        upstream = Upstream()
        result = await _call(tool, {"name": "x", "Content-Type": "text/evil"}, upstream)
        assert result.is_error  # not an argument at all
        result = await _call(tool, {"name": "x"}, upstream)
        assert upstream.requests[-1].headers["content-type"] == "application/json"


# --------------------------------------------------------------------------- auth


def _alice() -> Principal:
    return Principal(
        subject="alice",
        issuer="https://idp.example.com",
        token="alice-access-token",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def _obo(**fields: Any) -> RegisteredServer:
    return _server(
        auth_type="obo",
        oauth_token_url="https://idp.example.com/token",
        oauth_client_id="hub",
        oauth_client_secret="hub-secret",
        obo_audience="pets-api",
        **fields,
    )


class TestAuth:
    async def test_query_api_key_overrides_an_argument_of_the_same_name(self):
        upstream = Upstream()
        server = _server(
            auth_type="api_key", api_key_name="key", api_key_in="query", api_key_value="s3cret"
        )
        await _call(
            _query_tool(_param("key"), _param("q")),
            {"key": "attacker", "q": "x"},
            upstream,
            server=server,
        )
        assert upstream.requests[0].url.query.decode() == "q=x&key=s3cret"

    async def test_header_api_key_overrides_an_argument_of_the_same_name(self):
        upstream = Upstream()
        server = _server(
            auth_type="api_key", api_key_name="X-Key", api_key_in="header", api_key_value="s3cret"
        )
        await _call(
            _query_tool(_param("x-key", "header")), {"x-key": "attacker"}, upstream, server=server
        )
        assert upstream.requests[0].headers.get_list("x-key") == ["s3cret"]

    async def test_on_behalf_of_uses_the_callers_token(self):
        exchanges: list[Any] = []

        async def exchange(request, **kwargs):
            exchanges.append(request)
            return ExchangedToken(access_token="downstream-token", expires_in=300)

        auth = functools.partial(apply_server_auth, exchange=exchange, obo_cache=OBOTokenCache())
        upstream = Upstream()
        result = await _call(
            _query_tool(), {}, upstream, server=_obo(), caller=_alice(), apply_auth=auth
        )
        assert not result.is_error, _text(result)
        assert upstream.requests[0].headers["authorization"] == "Bearer downstream-token"
        assert exchanges

    async def test_on_behalf_of_fails_closed(self):
        async def exchange(request, **kwargs):
            raise TokenExchangeError("idp said no")

        auth = functools.partial(apply_server_auth, exchange=exchange, obo_cache=OBOTokenCache())
        result = await _call(
            _query_tool(), {}, _never, server=_obo(), caller=_alice(), apply_auth=auth
        )
        assert result.is_error
        assert "upstream API was not called" in _text(result)

    async def test_on_behalf_of_without_a_user_needs_authentication(self):
        auth = functools.partial(apply_server_auth, obo_cache=OBOTokenCache())
        result = await _call(
            _query_tool(), {}, _never, server=_obo(), caller=Principal.anonymous(), apply_auth=auth
        )
        assert result.is_error
        assert result.needs_authentication


# --------------------------------------------------------------------------- redirects


def _redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


class TestRedirects:
    async def test_same_origin_chain_is_followed_with_credentials(self):
        upstream = Upstream(
            _redirect("/v1/t2"),
            _redirect("https://api.example.com/v1/t3", 307),
            httpx.Response(200, json={"ok": True}),
        )
        server = _server(auth_type="bearer", bearer_token="tok")
        result = await _call(_query_tool(), {}, upstream, server=server)
        assert not result.is_error, _text(result)
        assert [r.url.path for r in upstream.requests] == ["/v1/t", "/v1/t2", "/v1/t3"]
        assert all(r.headers["authorization"] == "Bearer tok" for r in upstream.requests)

    @pytest.mark.parametrize(
        "target",
        [
            "https://evil.example.com/steal",
            "http://api.example.com/v1/t2",  # scheme change
            "https://api.example.com:8443/v1/t2",  # port change
            "//evil.example.com/x",
        ],
    )
    async def test_cross_origin_is_refused_and_never_requested(self, target):
        upstream = Upstream(_redirect(target))
        result = await _call(
            _query_tool(), {}, upstream, server=_server(auth_type="bearer", bearer_token="tok")
        )
        assert result.is_error
        assert len(upstream.requests) == 1
        expected = httpx.URL("https://api.example.com/v1/t").join(target)
        assert str(expected) in _text(result)

    async def test_chain_over_the_cap(self):
        upstream = Upstream(*[_redirect(f"/v1/t{i}") for i in range(10)])
        result = await _call(_query_tool(), {}, upstream)
        assert result.is_error and "redirect" in _text(result).lower()
        assert len(upstream.requests) == LIMITS.max_redirects + 1

    async def test_host_re_resolving_to_a_forbidden_address_mid_chain(self, monkeypatch):
        answers = iter(["93.184.216.34", None])

        async def resolve(host: str, *, allow_private_networks: bool = False) -> str | None:
            return next(answers)

        monkeypatch.setattr(executor, "resolve_pinned_ip", resolve)
        upstream = Upstream(_redirect("/v1/t2"), httpx.Response(200))
        result = await _call(_query_tool(), {}, upstream)
        assert result.is_error
        assert len(upstream.requests) == 1

    async def test_see_other_after_post_becomes_a_get_without_a_body(self):
        upstream = Upstream(_redirect("/v1/done", 303), httpx.Response(200, json={}))
        await _call(_body_tool(OBJECT), {"name": "x"}, upstream)
        second = upstream.requests[1]
        assert second.method == "GET" and second.content == b""
        assert "content-type" not in second.headers

    async def test_307_keeps_the_method_and_body(self):
        upstream = Upstream(_redirect("/v1/again", 307), httpx.Response(200, json={}))
        await _call(_body_tool(OBJECT), {"name": "x"}, upstream)
        assert upstream.requests[1].method == "POST"
        assert json.loads(upstream.requests[1].content) == {"name": "x"}

    async def test_forbidden_first_hop(self, monkeypatch):
        async def resolve(host: str, *, allow_private_networks: bool = False) -> str | None:
            return None

        monkeypatch.setattr(executor, "resolve_pinned_ip", resolve)
        result = await _call(_query_tool(), {}, _never)
        assert result.is_error


# --------------------------------------------------------------------------- responses


def _get_tool(schema: Any = None, media: str = "application/json") -> Tool:
    responses = {"200": {"description": "ok"}}
    if schema is not None:
        responses["200"]["content"] = {media: {"schema": schema}}
    return _tool({"/t": {"get": {"responses": responses}}}, "GET", "/t")


class TestResponses:
    async def test_object_json(self):
        body = {"name": "rex", "id": 1}
        result = await _call(_get_tool(OBJECT), {}, Upstream(httpx.Response(200, json=body)))
        assert not result.is_error
        assert result.structured == body
        assert json.loads(_text(result)) == body

    async def test_array_json_is_wrapped(self):
        result = await _call(_get_tool(ARRAY), {}, Upstream(httpx.Response(200, json=["a"])))
        assert result.structured == {"result": ["a"]}

    async def test_json_that_breaks_the_declared_schema_is_text_only(self):
        result = await _call(
            _get_tool(OBJECT), {}, Upstream(httpx.Response(200, json={"id": "not an int"}))
        )
        assert not result.is_error
        assert result.structured is None
        assert "not an int" in _text(result)

    async def test_json_without_a_declared_schema_is_text_only(self):
        result = await _call(_get_tool(), {}, Upstream(httpx.Response(200, json={"a": 1})))
        assert result.structured is None and json.loads(_text(result)) == {"a": 1}

    async def test_invalid_json_is_text(self):
        response = httpx.Response(
            200, content=b"{nope", headers={"Content-Type": "application/json"}
        )
        result = await _call(_get_tool(OBJECT), {}, Upstream(response))
        assert not result.is_error and _text(result) == "{nope" and result.structured is None

    async def test_text(self):
        response = httpx.Response(
            200, content="héllo".encode(), headers={"Content-Type": "text/plain; charset=utf-8"}
        )
        result = await _call(_get_tool(), {}, Upstream(response))
        assert _text(result) == "héllo"

    async def test_binary_is_an_embedded_blob(self):
        response = httpx.Response(
            200, content=b"\x89PNG\x00", headers={"Content-Type": "image/png"}
        )
        result = await _call(_get_tool(), {}, Upstream(response))
        (item,) = result.content
        assert item["type"] == "resource"
        assert item["resource"]["mimeType"] == "image/png"
        assert base64.b64decode(item["resource"]["blob"]) == b"\x89PNG\x00"

    async def test_no_content(self):
        result = await _call(_get_tool(), {}, Upstream(httpx.Response(204)))
        assert not result.is_error

    async def test_error_status(self):
        response = httpx.Response(404, json={"error": "no such pet"})
        result = await _call(_get_tool(OBJECT), {}, Upstream(response))
        assert result.is_error
        assert "404" in _text(result) and "no such pet" in _text(result)
        assert result.structured is None

    async def test_long_error_body_is_truncated(self):
        result = await _call(_get_tool(), {}, Upstream(httpx.Response(500, text="x" * 100_000)))
        assert len(_text(result)) < 5_000

    async def test_declared_oversize_response(self):
        response = httpx.Response(
            200,
            content=b"{}",
            headers={"Content-Length": "2000000", "Content-Type": "application/json"},
        )
        result = await _call(_get_tool(), {}, Upstream(response))
        assert result.is_error and "max_response_bytes" in _text(result)

    async def test_streamed_oversize_response(self):
        async def chunks():
            for _ in range(2000):
                yield b"x" * 1024

        result = await _call(_get_tool(), {}, Upstream(httpx.Response(200, content=chunks())))
        assert result.is_error and "max_response_bytes" in _text(result)

    async def test_timeout(self):
        import asyncio

        async def slow():
            await asyncio.sleep(5)
            yield b"{}"

        limits = CallLimits(
            timeout_seconds=0.2,
            max_response_bytes=1000,
            max_redirects=1,
            allow_private_networks=False,
        )
        start = time.monotonic()
        result = await _call(
            _get_tool(), {}, Upstream(httpx.Response(200, content=slow())), limits=limits
        )
        assert result.is_error and "timed out" in _text(result)
        assert time.monotonic() - start < 2

    async def test_transport_error_never_echoes_the_url(self):
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot connect to {request.url}", request=request)

        server = _server(
            auth_type="api_key", api_key_name="key", api_key_in="query", api_key_value="s3cret"
        )
        result = await _call(_get_tool(), {}, refuse, server=server)
        assert result.is_error
        assert "s3cret" not in _text(result)

    async def test_mcp_shape(self):
        result = await _call(
            _get_tool(OBJECT), {}, Upstream(httpx.Response(200, json={"name": "a"}))
        )
        shaped = result.to_mcp()
        assert shaped["isError"] is False
        assert shaped["structuredContent"] == {"name": "a"}
        assert shaped["content"][0]["type"] == "text"


class TestDefaultClient:
    async def test_without_an_injected_client_the_ssrf_pinned_one_is_used(self, monkeypatch):
        # The explicit hop check passes (stubbed public); the pinned transport then
        # refuses loopback itself, before any connection.
        result = await call_tool(
            _get_tool(),
            {},
            server=_server(openapi_base_url="http://127.0.0.1:9/v1"),
            caller=SERVICE_IDENTITY,
            limits=LIMITS,
        )
        assert result.is_error


class TestCoverageGaps:
    async def test_pattern_properties_validate_matching_keys(self):
        body = {"type": "object", "patternProperties": {"^a+$": {"type": "integer"}}}
        tool = _tool(
            {"/t": {"post": _op(requestBody={"content": {"application/json": {"schema": body}}})}},
            "POST",
            "/t",
        )
        result = await _call(tool, {"body": {"aa": "not an int"}}, _never)
        assert result.is_error and "aa" in _text(result)

    async def test_uncompilable_pattern_property_is_skipped(self):
        body = {"type": "object", "patternProperties": {r"^(a)\1$": {"type": "integer"}}}
        tool = _tool(
            {"/t": {"post": _op(requestBody={"content": {"application/json": {"schema": body}}})}},
            "POST",
            "/t",
        )
        upstream = Upstream()
        result = await _call(tool, {"body": {"aa": "x"}}, upstream)
        assert not result.is_error and len(upstream.requests) == 1

    async def test_array_path_value_is_comma_joined_per_item(self):
        param = _param("ids", "path", required=True, schema=ARRAY)
        tool = _tool({"/things/{ids}": {"get": _op(parameters=[param])}}, "GET", "/things/{ids}")
        upstream = Upstream()
        await _call(tool, {"ids": ["a/b", "c"]}, upstream)
        assert upstream.requests[0].url.raw_path.decode() == "/v1/things/a%2Fb,c"

    def test_limits_from_settings(self):
        from mcp_hub.config import SecurityConfig, Settings

        settings = Settings(security=SecurityConfig(allow_private_networks=True))
        assert CallLimits.from_settings(settings) == CallLimits(
            timeout_seconds=30,
            max_response_bytes=1024 * 1024,
            max_redirects=5,
            allow_private_networks=True,
        )
