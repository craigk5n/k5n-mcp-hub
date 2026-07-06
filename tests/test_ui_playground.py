import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.models import RegisteredServer
from mcp_hub.registry.service import Registry


def test_playground_returns_200_with_form() -> None:
    app = create_app()
    registry: Registry = app.state.registry

    server = RegisteredServer(
        id="test-server-playground",
        url="http://localhost:8000/mcp",
        name="Test Server",
    )
    asyncio.run(registry.register(server))

    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/server/test-server-playground/playground")

    assert response.status_code == 200
    assert response.headers.get("content-type") == "text/html; charset=utf-8"
    assert 'hx-post="/ui/server/test-server-playground/playground"' in response.text
    assert 'hx-swap="outerHTML"' in response.text
    assert 'name="request_body"' in response.text
    assert 'name="session_id"' in response.text
    assert 'name="protocol_version"' in response.text
    assert 'name="accept_sse"' in response.text


def test_playground_form_has_htmx_attributes() -> None:
    app = create_app()
    registry: Registry = app.state.registry

    server = RegisteredServer(
        id="test-server-htmx",
        url="http://localhost:8000/mcp",
        name="Test Server",
    )
    asyncio.run(registry.register(server))

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/ui/server/test-server-htmx/playground")

    assert 'hx-post="/ui/server/test-server-htmx/playground"' in response.text
    assert 'hx-swap="outerHTML"' in response.text


def test_playground_has_all_named_inputs() -> None:
    app = create_app()
    registry: Registry = app.state.registry

    server = RegisteredServer(
        id="test-server-inputs",
        url="http://localhost:8000/mcp",
        name="Test Server",
    )
    asyncio.run(registry.register(server))

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/ui/server/test-server-inputs/playground")

    assert 'name="request_body"' in response.text
    assert 'name="session_id"' in response.text
    assert 'name="protocol_version"' in response.text
    assert 'name="accept_sse"' in response.text
    assert 'name="server_id"' in response.text
    assert 'name="url"' in response.text


def test_playground_has_quick_action_buttons() -> None:
    app = create_app()
    registry: Registry = app.state.registry

    server = RegisteredServer(
        id="test-server-buttons",
        url="http://localhost:8000/mcp",
        name="Test Server",
    )
    asyncio.run(registry.register(server))

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/ui/server/test-server-buttons/playground")

    assert "initialize" in response.text and 'type="button"' in response.text
    assert "tools/list" in response.text
    assert "tools/call" in response.text
    assert "ping" in response.text


def test_playground_unknown_server_returns_404() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/server/nonexistent-server-id/playground")

    assert response.status_code == 404


def test_playground_post_renders_result_panels_when_inputs_set() -> None:
    app = create_app()
    registry: Registry = app.state.registry

    server = RegisteredServer(
        id="test-server-post",
        url="http://invalid-host-that-will-fail:12345/mcp",
        name="Test Server",
    )
    asyncio.run(registry.register(server))

    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/ui/server/test-server-post/playground",
        data={
            "server_id": "test-server-post",
            "url": "http://invalid-host-that-will-fail:12345/mcp",
            "request_body": '{"jsonrpc":"2.0","id":1,"method":"ping"}',
            "session_id": "test-session",
            "protocol_version": "2024-11-05",
            "accept_sse": "on",
        },
    )

    assert response.status_code == 200
    assert '<h2 class="text-lg font-semibold mb-4">Request</h2>' in response.text
    assert '<h2 class="text-lg font-semibold mb-4">Response</h2>' in response.text


def test_playground_template_renders_parsed_body_when_provided() -> None:
    app = create_app()
    templates = app.state.templates

    template = templates.get_template("playground.html")
    html = asyncio.run(
        template.render_async(
            server_id="test-server",
            url="http://localhost:8000/mcp",
            request_body='{"jsonrpc":"2.0","id":1,"method":"ping"}',
            session_id="",
            protocol_version="",
            accept_sse=False,
            error="",
            request_headers="Content-Type: application/json",
            response_status="200",
            response_headers="",
            response_body='{"jsonrpc":"2.0","result":{}}',
            parsed_body='{\n  "jsonrpc": "2.0",\n  "result": {}\n}',
            auth_hint="",
        )
    )

    assert '<h2 class="text-lg font-semibold mb-4">Parsed Body</h2>' in html


def test_playground_renders_auth_hint_when_set() -> None:
    app = create_app()
    registry: Registry = app.state.registry
    templates = app.state.templates

    server = RegisteredServer(
        id="test-server-auth",
        url="http://localhost:8000/mcp",
        name="Test Server",
    )
    asyncio.run(registry.register(server))

    template = templates.get_template("playground.html")
    html_cached = asyncio.run(
        template.render_async(
            server_id=server.id,
            url=server.url,
            request_body="",
            session_id="",
            protocol_version="",
            accept_sse=False,
            error="",
            auth_hint="Authentication required: please provide valid credentials",
        )
    )

    assert "Authentication required: please provide valid credentials" in html_cached
    assert "bg-yellow-100" in html_cached


def test_playground_no_result_panels_on_initial_get() -> None:
    app = create_app()
    registry: Registry = app.state.registry

    server = RegisteredServer(
        id="test-server-nopanels",
        url="http://localhost:8000/mcp",
        name="Test Server",
    )
    asyncio.run(registry.register(server))

    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/server/test-server-nopanels/playground")

    assert response.status_code == 200
    assert '<h2 class="text-lg font-semibold mb-4">Request</h2>' not in response.text
    assert '<h2 class="text-lg font-semibold mb-4">Response</h2>' not in response.text
    assert '<h2 class="text-lg font-semibold mb-4">Parsed Body</h2>' not in response.text


def test_playground_empty_body_returns_error_without_backend_call() -> None:
    app = create_app()
    registry: Registry = app.state.registry

    server = RegisteredServer(
        id="test-server-empty-body",
        url="http://localhost:8000/mcp",
        name="Test Server",
    )
    asyncio.run(registry.register(server))

    client = TestClient(app, raise_server_exceptions=False)

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client.post = AsyncMock()

        response = client.post(
            "/ui/server/test-server-empty-body/playground",
            data={
                "request_body": "",
                "session_id": "",
                "protocol_version": "",
            },
        )

        assert response.status_code == 200
        assert "request body is required" in response.text
        mock_client.post.assert_not_called()


def test_playground_valid_initialize_returns_response_status() -> None:
    app = create_app()
    registry: Registry = app.state.registry

    server = RegisteredServer(
        id="test-server-initialize",
        url="http://localhost:8000/mcp",
        name="Test Server",
    )
    asyncio.run(registry.register(server))

    client = TestClient(app, raise_server_exceptions=False)

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.text = '{"jsonrpc":"2.0","id":1,"result":{}}'
        mock_client.post = AsyncMock(return_value=mock_resp)

        response = client.post(
            "/ui/server/test-server-initialize/playground",
            data={
                "request_body": '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
                "session_id": "",
                "protocol_version": "",
            },
        )

        assert response.status_code == 200
        assert "200" in response.text
        assert "Response" in response.text


def test_playground_401_returns_auth_hint() -> None:
    app = create_app()
    registry: Registry = app.state.registry

    server = RegisteredServer(
        id="test-server-401",
        url="http://localhost:8000/mcp",
        name="Test Server",
    )
    asyncio.run(registry.register(server))

    client = TestClient(app, raise_server_exceptions=False)

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_resp = AsyncMock()
        mock_resp.status_code = 401
        mock_resp.headers = {
            "content-type": "application/json",
            "WWW-Authenticate": 'Bearer error="invalid_token", error_description="Token expired"',
        }
        mock_resp.text = (
            '{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"Invalid token"}}'
        )
        mock_client.post = AsyncMock(return_value=mock_resp)

        response = client.post(
            "/ui/server/test-server-401/playground",
            data={
                "request_body": '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
                "session_id": "",
                "protocol_version": "",
            },
        )

        assert response.status_code == 200
        assert "invalid_token" in response.text or "WWW-Authenticate" in response.text
        assert "401" in response.text
