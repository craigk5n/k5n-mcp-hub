import pytest

from mcp_hub.routes.ui_invoke import build_tool_args, coerce_form_value


class TestCoerceFormValue:
    def test_true_lowercase(self) -> None:
        assert coerce_form_value("true") is True

    def test_true_uppercase(self) -> None:
        assert coerce_form_value("TRUE") is True

    def test_true_mixed_case(self) -> None:
        assert coerce_form_value("True") is True

    def test_false_lowercase(self) -> None:
        assert coerce_form_value("false") is False

    def test_false_uppercase(self) -> None:
        assert coerce_form_value("FALSE") is False

    def test_false_mixed_case(self) -> None:
        assert coerce_form_value("False") is False

    def test_integer(self) -> None:
        assert coerce_form_value("42") == 42
        assert coerce_form_value("0") == 0
        assert coerce_form_value("-10") == -10

    def test_float(self) -> None:
        assert coerce_form_value("3.14") == 3.14
        assert coerce_form_value("0.5") == 0.5
        assert coerce_form_value("-2.5") == -2.5

    def test_string_unchanged(self) -> None:
        assert coerce_form_value("hi") == "hi"
        assert coerce_form_value("hello world") == "hello world"
        assert coerce_form_value("") == ""


class TestBuildToolArgs:
    def test_basic_coercion(self) -> None:
        form = {"a": ["1"], "b": ["true"], "c": ["x"]}
        result = build_tool_args(form)
        assert result == {"a": 1, "b": True, "c": "x"}

    def test_json_marker(self) -> None:
        form = {"__json__obj": ["1"], "obj": ['{"k":1}']}
        result = build_tool_args(form)
        assert result == {"obj": {"k": 1}}

    def test_json_marker_invalid_json_raises(self) -> None:
        form = {"__json__data": ["invalid"], "data": ["something"]}
        with pytest.raises(ValueError, match="Invalid JSON for data"):
            build_tool_args(form)

    def test_ignore_keys(self) -> None:
        form = {"a": ["1"], "b": ["2"], "c": ["3"]}
        result = build_tool_args(form, ignore={"b"})
        assert result == {"a": 1, "c": 3}

    def test_ignore_none(self) -> None:
        form = {"a": ["1"]}
        result = build_tool_args(form, ignore=None)
        assert result == {"a": 1}

    def test_empty_value_skipped(self) -> None:
        form = {"a": [""], "b": ["value"]}
        result = build_tool_args(form)
        assert result == {"b": "value"}

    def test_empty_list_skipped(self) -> None:
        form = {"a": [], "b": ["value"]}
        result = build_tool_args(form)
        assert result == {"b": "value"}

    def test_json_auto_parse_object(self) -> None:
        form = {"data": ['{"key": "value"}']}
        result = build_tool_args(form)
        assert result == {"data": {"key": "value"}}

    def test_json_auto_parse_array(self) -> None:
        form = {"data": ["[1, 2, 3]"]}
        result = build_tool_args(form)
        assert result == {"data": [1, 2, 3]}

    def test_json_auto_parse_fallback_on_invalid(self) -> None:
        form = {"data": ["{invalid}"]}
        result = build_tool_args(form)
        assert result == {"data": "{invalid}"}

    def test_json_marker_with_actual_json_value(self) -> None:
        form = {"__json__config": ["1"], "config": ['{"enabled": true}']}
        result = build_tool_args(form)
        assert result == {"config": {"enabled": True}}

    def test_multiple_json_markers(self) -> None:
        form = {
            "__json__obj": ["1"],
            "__json__arr": ["1"],
            "obj": ['{"a":1}'],
            "arr": ["[1,2,3]"],
        }
        result = build_tool_args(form)
        assert result == {"obj": {"a": 1}, "arr": [1, 2, 3]}

    def test_boolean_coercion_various_cases(self) -> None:
        form = {"t": ["True"], "f": ["False"], "u": ["TRUE"], "l": ["FALSE"]}
        result = build_tool_args(form)
        assert result == {"t": True, "f": False, "u": True, "l": False}

    def test_numeric_strings(self) -> None:
        form = {"int": ["123"], "neg": ["-45"], "float": ["1.5"]}
        result = build_tool_args(form)
        assert result == {"int": 123, "neg": -45, "float": 1.5}

    def test_whitespace_stripped_for_json_detection(self) -> None:
        form = {"data": ['  {"key": "value"}']}
        result = build_tool_args(form)
        assert result == {"data": {"key": "value"}}

        form2 = {"data": ["  [1, 2, 3]"]}
        result2 = build_tool_args(form2)
        assert result2 == {"data": [1, 2, 3]}


class TestInvokeFragment:
    def test_build_success_fragment(self) -> None:
        from mcp_hub.routes.ui_invoke import _build_success_fragment

        result = _build_success_fragment("Hello World")

        assert "bg-green-50" in result
        assert "Hello World" in result
        # New structure: a result container + output <pre>, plus the Pretty/Raw toggle
        # and copy hooks the parent page wires up.
        assert "data-tool-result" in result
        assert "data-tool-output" in result
        assert "data-format-toggle" in result
        assert "data-output-copy" in result
        # Output must wrap/scroll rather than overflow the panel.
        assert "overflow-auto" in result
        assert "break-words" in result

    def test_build_error_fragment(self) -> None:
        from mcp_hub.routes.ui_invoke import _build_error_fragment

        result = _build_error_fragment("Something went wrong")

        assert "bg-red-50" in result
        assert "text-red-800" in result
        assert "Something went wrong" in result
        assert "data-tool-output" in result

    def test_escape_html_in_message(self) -> None:
        from mcp_hub.routes.ui_invoke import _build_success_fragment

        result = _build_success_fragment("<script>alert('xss')</script>")

        assert "<script>" not in result
        assert "&lt;script&gt;" in result or "&#34;script&#34;" in result


def make_basic_auth_header(user: str, password: str) -> dict[str, str]:
    import base64

    credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


class TestInvokeRoute:
    def test_invoke_route_404_when_server_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from mcp_hub.app import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/ui/invoke/nonexistent-server/my-tool",
            headers=make_basic_auth_header("admin", "admin123"),
        )

        assert response.status_code == 404

    def test_invoke_route_accepts_tool_name_with_slash(self) -> None:
        # Regression: MCP tool names may contain "/" (e.g. "webcalendar/list-events").
        # The route must capture the whole name and reach the handler instead of 404ing at
        # the router. An unknown server proves routing matched: the handler's own 404
        # (detail "Server not found") differs from a router miss (detail "Not Found").
        from fastapi.testclient import TestClient

        from mcp_hub.app import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post("/ui/invoke/nonexistent-server/webcalendar/list-events")

        assert response.status_code == 404
        assert response.json()["detail"] == "Server not found"


class TestStatelessInvoke:
    """2026-07-28 servers get a single stateless tools/call POST — no initialize,
    no notifications/initialized, no Mcp-Session-Id."""

    def _make_app(self):
        from mcp_hub.app import create_app
        from mcp_hub.config import SecurityConfig, Settings, StorageConfig

        return create_app(
            Settings(
                storage=StorageConfig(type="inmemory"),
                security=SecurityConfig(allow_private_networks=True),
            )
        )

    async def _register(self, app, fake, protocol_version: str) -> None:
        from mcp_hub.models.server import RegisteredServer

        await app.state.registry.register(
            RegisteredServer(
                id="fake-srv",
                url=f"{fake.base_url}/",
                name="Fake",
                mcp_protocol_version=protocol_version,
            )
        )

    @pytest.mark.asyncio
    async def test_stateless_server_invoked_with_single_post(self, fake_stateless_mcp_server):
        import httpx

        app = self._make_app()
        await self._register(app, fake_stateless_mcp_server, "2026-07-28")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/ui/invoke/fake-srv/echo", data={"msg": "hi"})

        assert response.status_code == 200
        assert "ok" in response.text

        assert fake_stateless_mcp_server.handler_call_count == 1, "must be one POST, no handshake"
        body = fake_stateless_mcp_server.last_request_body
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "echo"
        assert body["params"]["arguments"] == {"msg": "hi"}
        meta = body["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"

        headers = fake_stateless_mcp_server.last_request_headers
        assert headers.get("Mcp-Method") == "tools/call"
        assert headers.get("MCP-Protocol-Version") == "2026-07-28"
        assert "Mcp-Session-Id" not in headers

    @pytest.mark.asyncio
    async def test_legacy_server_keeps_three_step_flow(self, fake_mcp_server):
        import httpx

        app = self._make_app()
        await self._register(app, fake_mcp_server, "2025-11-25")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/ui/invoke/fake-srv/echo", data={"msg": "hi"})

        assert response.status_code == 200
        assert "ok" in response.text
        # initialize + notifications/initialized + tools/call
        assert fake_mcp_server.handler_call_count == 3
        body = fake_mcp_server.last_request_body
        assert body["method"] == "tools/call"
        assert "_meta" not in body["params"]
