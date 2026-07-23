import asyncio

import pytest

from mcp_hub.models import RegisteredServer
from mcp_hub.registry import Registry
from mcp_hub.storage.memory import InMemoryStorage


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def registry(storage: InMemoryStorage) -> Registry:
    return Registry(storage)


@pytest.mark.asyncio
async def test_register_twice_preserves_created_at(registry: Registry) -> None:
    server = RegisteredServer(id="srv1", url="http://localhost:8000", name="Test")
    result1 = await registry.register(server)
    created_at_1 = result1.created_at
    updated_at_1 = result1.updated_at

    assert created_at_1 is not None
    assert updated_at_1 is not None

    await asyncio.sleep(0.01)

    server2 = RegisteredServer(id="srv1", url="http://localhost:8000", name="Test")
    result2 = await registry.register(server2)

    assert result2.created_at == created_at_1
    assert result2.updated_at is not None
    assert result2.updated_at > updated_at_1


@pytest.mark.asyncio
async def test_register_merges_capability_cache(registry: Registry) -> None:
    existing = RegisteredServer(
        id="srv2", url="http://localhost:8001", name="Test", tools=[{"name": "foo"}]
    )
    await registry.register(existing)

    new_server = RegisteredServer(id="srv2", url="http://localhost:8001", name="Test")
    result = await registry.register(new_server)

    assert result.tools == [{"name": "foo"}]


@pytest.mark.asyncio
async def test_update_health_and_uptime(registry: Registry) -> None:
    server = RegisteredServer(id="srv3", url="http://localhost:8002", name="Test")
    await registry.register(server)

    # Uptime is now hub-tracked from a `healthy_since` timestamp (the passed `uptime` is
    # ignored). First healthy check sets healthy_since; uptime starts near zero and grows.
    await registry.update_health_and_uptime(
        "srv3", healthy=True, consecutive_fails=0, uptime=3600.0
    )
    result = await registry.get("srv3")
    assert result is not None
    assert result.healthy is True
    assert result.consecutive_fails == 0
    assert result.healthy_since is not None
    assert result.uptime_seconds >= 0.0
    assert result.last_checked is not None
    first_since = result.healthy_since

    # A second healthy check keeps the same healthy_since (continuous uptime).
    await registry.update_health_and_uptime("srv3", healthy=True, consecutive_fails=0, uptime=0.0)
    result = await registry.get("srv3")
    assert result is not None and result.healthy_since == first_since

    # A failed check resets uptime and clears healthy_since.
    await registry.update_health_and_uptime("srv3", healthy=False, consecutive_fails=1, uptime=0.0)
    result = await registry.get("srv3")
    assert result is not None
    assert result.healthy is False
    assert result.healthy_since is None
    assert result.uptime_seconds == 0.0


@pytest.mark.asyncio
async def test_set_supports_health_endpoint(registry: Registry) -> None:
    server = RegisteredServer(id="srv4", url="http://localhost:8003", name="Test")
    await registry.register(server)

    await registry.set_supports_health_endpoint("srv4", False)

    result = await registry.get("srv4")
    assert result is not None
    assert result.supports_health_endpoint is False
