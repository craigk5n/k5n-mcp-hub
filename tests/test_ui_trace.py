import pytest
from fastapi.testclient import TestClient

from devhub.app import create_app
from devhub.models import RegisteredServer
from devhub.registry.service import Registry
from devhub.trace.recorder import TraceEntry, TraceRecorder


def test_ui_trace_empty_id_returns_400() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/server//trace")

    assert response.status_code == 400


def test_ui_trace_unknown_id_returns_200_with_empty_entries() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/server/nonexistent-server/trace")

    assert response.status_code == 200
    assert response.headers.get("content-type") == "text/html; charset=utf-8"
    assert "nonexistent-server" in response.text
    assert "No trace entries" in response.text
    assert "Verbose: False" in response.text


def test_ui_trace_known_server_with_trace_entries() -> None:
    import asyncio
    from datetime import datetime, timezone

    app = create_app()
    registry: Registry = app.state.registry
    trace_recorder: TraceRecorder = app.state.trace_recorder

    server = RegisteredServer(
        id="test-server-trace",
        url="http://localhost:8000/mcp",
        name="Test Server",
    )
    asyncio.run(registry.register(server))

    entry = TraceEntry(
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        server_id="test-server-trace",
        operation="proxy",
        http_method="GET",
        url="http://example.com/api",
        outbound_url="http://backend/api",
        status=200,
        duration_ms=150,
        error="",
        request_body="request",
        response_body="response",
    )
    trace_recorder.add(entry)

    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/server/test-server-trace/trace")

    assert response.status_code == 200
    assert "test-server-trace" in response.text
    assert "proxy" in response.text
    assert "GET" in response.text
    assert "200" in response.text
    assert "150ms" in response.text


def test_ui_trace_verbose_flag_from_server() -> None:
    import asyncio
    from datetime import datetime, timezone

    app = create_app()
    registry: Registry = app.state.registry

    server = RegisteredServer(
        id="verbose-server",
        url="http://localhost:8000/mcp",
        name="Verbose Server",
        trace_verbose=True,
    )
    asyncio.run(registry.register(server))

    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/server/verbose-server/trace")

    assert response.status_code == 200
    assert "Verbose: True" in response.text


def test_ui_trace_non_verbose_flag_from_server() -> None:
    import asyncio
    from datetime import datetime, timezone

    app = create_app()
    registry: Registry = app.state.registry

    server = RegisteredServer(
        id="non-verbose-server",
        url="http://localhost:8000/mcp",
        name="Non Verbose Server",
        trace_verbose=False,
    )
    asyncio.run(registry.register(server))

    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/server/non-verbose-server/trace")

    assert response.status_code == 200
    assert "Verbose: False" in response.text


def test_ui_trace_verbose_shows_details() -> None:
    import asyncio
    from datetime import datetime, timezone

    app = create_app()
    registry: Registry = app.state.registry
    trace_recorder: TraceRecorder = app.state.trace_recorder

    server = RegisteredServer(
        id="verbose-details-server",
        url="http://localhost:8000/mcp",
        name="Verbose Details Server",
        trace_verbose=True,
    )
    asyncio.run(registry.register(server))

    entry = TraceEntry(
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        server_id="verbose-details-server",
        operation="proxy",
        http_method="GET",
        url="http://example.com/api",
        outbound_url="http://backend/api",
        status=200,
        duration_ms=150,
        error="",
        request_body='{"key": "value"}',
        response_body='{"result": "ok"}',
        request_headers={"Content-Type": "application/json"},
        response_headers={"Content-Type": "application/json"},
    )
    trace_recorder.add(entry)

    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/server/verbose-details-server/trace")

    assert response.status_code == 200
    assert "Request Body:" in response.text
    assert "Response Body:" in response.text


def test_ui_trace_non_verbose_hides_details() -> None:
    import asyncio
    from datetime import datetime, timezone

    app = create_app()
    registry: Registry = app.state.registry
    trace_recorder: TraceRecorder = app.state.trace_recorder

    server = RegisteredServer(
        id="quiet-server",
        url="http://localhost:8000/mcp",
        name="Quiet Server",
        trace_verbose=False,
    )
    asyncio.run(registry.register(server))

    entry = TraceEntry(
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        server_id="quiet-server",
        operation="proxy",
        http_method="GET",
        url="http://example.com/api",
        outbound_url="http://backend/api",
        status=200,
        duration_ms=150,
        error="",
        request_body='{"key": "value"}',
        response_body='{"result": "ok"}',
    )
    trace_recorder.add(entry)

    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/server/quiet-server/trace")

    assert response.status_code == 200
    assert "Request Body:" not in response.text
    assert "Response Body:" not in response.text


def test_ui_trace_clear_removes_entries() -> None:
    import asyncio
    from datetime import datetime, timezone

    app = create_app()
    registry: Registry = app.state.registry
    trace_recorder: TraceRecorder = app.state.trace_recorder

    server = RegisteredServer(
        id="clear-test-server",
        url="http://localhost:8000/mcp",
        name="Clear Test Server",
        trace_verbose=True,
    )
    asyncio.run(registry.register(server))

    entry1 = TraceEntry(
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        server_id="clear-test-server",
        operation="proxy",
        http_method="GET",
        url="http://example.com/api1",
        outbound_url="http://backend/api1",
        status=200,
        duration_ms=100,
        error="",
    )
    entry2 = TraceEntry(
        timestamp=datetime(2024, 1, 1, 12, 0, 1, tzinfo=timezone.utc),
        server_id="clear-test-server",
        operation="proxy",
        http_method="POST",
        url="http://example.com/api2",
        outbound_url="http://backend/api2",
        status=201,
        duration_ms=200,
        error="",
    )
    trace_recorder.add(entry1)
    trace_recorder.add(entry2)

    assert len(trace_recorder.list("clear-test-server")) == 2

    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/ui/server/clear-test-server/trace/clear")

    assert response.status_code == 200
    assert "clear-test-server" in response.text
    assert "No trace entries" in response.text
    assert len(trace_recorder.list("clear-test-server")) == 0
