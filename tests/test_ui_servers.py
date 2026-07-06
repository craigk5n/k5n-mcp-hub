import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.models import RegisteredServer
from mcp_hub.registry.service import Registry


def test_ui_servers_empty_registry_returns_200() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/servers")

    assert response.status_code == 200
    assert response.headers.get("content-type") == "text/html; charset=utf-8"


def test_ui_servers_registered_server_shows_id() -> None:
    import asyncio

    app = create_app()
    registry: Registry = app.state.registry

    server = RegisteredServer(
        id="test-server-1",
        url="http://localhost:8000/mcp",
        name="Test Server",
    )
    asyncio.run(registry.register(server))

    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/servers")

    assert response.status_code == 200
    assert response.headers.get("content-type") == "text/html; charset=utf-8"
    assert "test-server-1" in response.text


def test_ui_servers_background_probe_does_not_block_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    app = create_app()
    registry: Registry = app.state.registry

    server = RegisteredServer(
        id="test-server-probe",
        url="http://localhost:8000/mcp",
        name="Test Server",
        mcp_transport="",
    )

    probe_called = False

    async def mock_probe(*args: object, **kwargs: object) -> None:
        nonlocal probe_called
        probe_called = True

    import mcp_hub.routes.ui_servers as ui_servers_module

    monkeypatch.setattr(ui_servers_module, "_probe_all_servers_task", mock_probe)

    asyncio.run(registry.register(server))

    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/ui/servers")

    assert response.status_code == 200
    assert "test-server-probe" in response.text
