import httpx
import pytest

from mcp_hub.app import create_app
from mcp_hub.models.server import RegisteredServer


@pytest.fixture
async def app_client():
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield app, client


@pytest.mark.asyncio
async def test_toggle_verbose_enable_when_disabled(app_client) -> None:
    app, client = app_client
    server = RegisteredServer(
        id="server-1",
        url="http://example.com",
        name="Test Server",
        trace_verbose=False,
    )
    await app.state.registry.register(server)

    response = await client.post("/ui/server/server-1/trace/verbose")

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_toggle_verbose_disable_when_enabled(app_client) -> None:
    app, client = app_client
    server = RegisteredServer(
        id="server-2",
        url="http://example.com",
        name="Test Server",
        trace_verbose=True,
    )
    await app.state.registry.register(server)

    response = await client.post("/ui/server/server-2/trace/verbose")

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_double_toggle_returns_to_original_state(app_client) -> None:
    app, client = app_client
    server = RegisteredServer(
        id="server-3",
        url="http://example.com",
        name="Test Server",
        trace_verbose=False,
    )
    await app.state.registry.register(server)

    response1 = await client.post("/ui/server/server-3/trace/verbose")
    assert response1.status_code == 200

    response2 = await client.post("/ui/server/server-3/trace/verbose")
    assert response2.status_code == 200

    server_after = await app.state.registry.get("server-3")
    assert server_after is not None
    assert server_after.trace_verbose is False


@pytest.mark.asyncio
async def test_toggle_persists_in_registry(app_client) -> None:
    app, client = app_client
    server = RegisteredServer(
        id="server-4",
        url="http://example.com",
        name="Test Server",
        trace_verbose=False,
    )
    await app.state.registry.register(server)

    response = await client.post("/ui/server/server-4/trace/verbose")
    assert response.status_code == 200, response.text

    server_after = await app.state.registry.get("server-4")
    assert server_after is not None
    assert server_after.trace_verbose is True


@pytest.mark.asyncio
async def test_unknown_server_returns_404(app_client) -> None:
    _, client = app_client

    response = await client.post("/ui/server/nonexistent-server/trace/verbose")

    assert response.status_code == 404
    assert "Server not found" in response.text


@pytest.mark.asyncio
async def test_verbose_flag_reflected_in_response(app_client) -> None:
    app, client = app_client
    server = RegisteredServer(
        id="server-5",
        url="http://example.com",
        name="Test Server",
        trace_verbose=False,
    )
    await app.state.registry.register(server)

    response = await client.post("/ui/server/server-5/trace/verbose")

    assert response.status_code == 200
    assert "Verbose: True" in response.text
