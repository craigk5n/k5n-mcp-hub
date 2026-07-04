import json
import os
import pytest
from datetime import datetime, timezone
from unittest.mock import patch

from devhub.models import AgentCard, RegisteredAgent, RegisteredServer
from devhub.storage import InMemoryStorage, JSONFileStorage, StorageStrategy


def test_storage_strategy_is_runtime_checkable_protocol() -> None:
    assert isinstance(InMemoryStorage(), StorageStrategy)


@pytest.mark.asyncio
async def test_register_then_get_returns_equal_object() -> None:
    storage = InMemoryStorage()
    await storage.init()
    server = RegisteredServer(id="server-1", url="http://localhost:8000", name="Test Server")
    await storage.register(server)
    result = await storage.get("server-1")
    assert result is not None
    assert result.id == server.id
    assert result.url == server.url
    assert result.name == server.name


@pytest.mark.asyncio
async def test_register_twice_preserves_created_at_and_updates_updated_at() -> None:
    storage = InMemoryStorage()
    await storage.init()
    server1 = RegisteredServer(id="server-1", url="http://localhost:8000", name="Test Server")
    await storage.register(server1)
    result1 = await storage.get("server-1")
    assert result1 is not None
    created_at_1 = result1.created_at
    updated_at_1 = result1.updated_at
    assert created_at_1 is not None
    assert updated_at_1 is not None

    import asyncio

    await asyncio.sleep(0.01)

    server2 = RegisteredServer(id="server-1", url="http://localhost:8000", name="Updated Server")
    await storage.register(server2)
    result2 = await storage.get("server-1")
    assert result2 is not None
    assert result2.created_at is not None
    assert result2.updated_at is not None
    assert result2.created_at == created_at_1
    assert result2.updated_at > updated_at_1


@pytest.mark.asyncio
async def test_get_returns_deep_copy_mutating_returned_object_does_not_mutate_stored() -> None:
    storage = InMemoryStorage()
    await storage.init()
    server = RegisteredServer(id="server-1", url="http://localhost:8000", name="Test Server")
    await storage.register(server)
    result = await storage.get("server-1")
    assert result is not None
    original_name = result.name
    result.name = "Mutated Name"
    stored = await storage.get("server-1")
    assert stored is not None
    assert stored.name == original_name


@pytest.mark.asyncio
async def test_unregister_missing_raises_key_error() -> None:
    storage = InMemoryStorage()
    await storage.init()
    with pytest.raises(KeyError) as exc_info:
        await storage.unregister("missing")
    assert exc_info.value.args[0] == "missing"


@pytest.mark.asyncio
async def test_jsonfile_storage_register_creates_correct_structure(tmp_path) -> None:
    storage = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage.init()
    server = RegisteredServer(id="server-1", url="http://localhost:8000", name="Test Server")
    await storage.register(server)

    with open(storage._path, "r") as f:
        data = json.load(f)

    assert "version" in data
    assert "servers" in data
    assert "agents" in data
    assert set(data.keys()) == {"version", "servers", "agents"}
    assert data["version"] == 1
    assert len(data["servers"]) == 1


@pytest.mark.asyncio
async def test_jsonfile_storage_strips_volatile_fields(tmp_path) -> None:
    storage = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage.init()
    server = RegisteredServer(
        id="server-1",
        url="http://localhost:8000",
        name="Test Server",
        tools=[{"name": "tool1"}],
        last_capability_sync=datetime.now(timezone.utc),
        healthy=True,
        last_checked=datetime.now(timezone.utc),
        oauth_token_status="ok",
        oauth_token_error="some error",
    )
    await storage.register(server)

    with open(storage._path, "r") as f:
        data = json.load(f)

    persisted = data["servers"][0]
    assert persisted.get("tools") is None
    assert persisted.get("last_capability_sync") is None
    assert persisted.get("healthy") is False
    assert persisted.get("last_checked") is None
    assert persisted.get("oauth_token_status") == ""
    assert persisted.get("oauth_token_error") == ""


@pytest.mark.asyncio
async def test_jsonfile_storage_loads_bare_array(tmp_path) -> None:
    file_path = tmp_path / "mcp_servers.json"
    with open(file_path, "w") as f:
        json.dump([{"id": "x", "url": "http://x"}], f)

    storage = JSONFileStorage(str(file_path))
    await storage.init()
    servers = await storage.list()

    assert len(servers) == 1
    assert servers[0].id == "x"
    assert servers[0].url == "http://x"


@pytest.mark.asyncio
async def test_jsonfile_storage_loads_wrapped_format(tmp_path) -> None:
    file_path = tmp_path / "mcp_servers.json"
    with open(file_path, "w") as f:
        json.dump({"version": 1, "servers": [{"id": "x", "url": "http://x"}]}, f)

    storage = JSONFileStorage(str(file_path))
    await storage.init()
    servers = await storage.list()

    assert len(servers) == 1
    assert servers[0].id == "x"


@pytest.mark.asyncio
async def test_jsonfile_storage_atomic_write_safety(tmp_path) -> None:
    storage = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage.init()
    server = RegisteredServer(id="server-1", url="http://localhost:8000", name="Original")
    await storage.register(server)

    with open(storage._path, "r") as f:
        original_content = f.read()

    def raise_error(*args):
        raise OSError("simulated write failure")

    with patch.object(os, "replace", raise_error):
        server2 = RegisteredServer(id="server-2", url="http://localhost:8001", name="New")
        with pytest.raises(OSError):
            await storage.register(server2)

    with open(storage._path, "r") as f:
        current_content = f.read()

    assert current_content == original_content

    temp_files = list(tmp_path.glob("tmp*"))
    assert len(temp_files) == 0


@pytest.mark.asyncio
async def test_jsonfile_storage_file_mode(tmp_path) -> None:
    storage = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage.init()
    server = RegisteredServer(id="server-1", url="http://localhost:8000", name="Test Server")
    await storage.register(server)

    mode = os.stat(storage._path).st_mode & 0o777
    assert mode == 0o600


@pytest.mark.asyncio
async def test_jsonfile_storage_parent_dir_mode(tmp_path) -> None:
    nested_dir = tmp_path / "nested" / "dir"
    storage = JSONFileStorage(str(nested_dir / "mcp_servers.json"))
    await storage.init()
    server = RegisteredServer(id="server-1", url="http://localhost:8000", name="Test Server")
    await storage.register(server)

    mode = os.stat(nested_dir).st_mode & 0o777
    assert mode == 0o700


@pytest.mark.asyncio
async def test_jsonfile_storage_preserves_created_at_updates_updated_at(tmp_path) -> None:
    storage = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage.init()
    server1 = RegisteredServer(id="server-1", url="http://localhost:8000", name="Test Server")
    await storage.register(server1)
    result1 = await storage.get("server-1")
    assert result1 is not None
    created_at_1 = result1.created_at
    updated_at_1 = result1.updated_at
    assert created_at_1 is not None
    assert updated_at_1 is not None

    import asyncio

    await asyncio.sleep(0.01)

    server2 = RegisteredServer(id="server-1", url="http://localhost:8000", name="Updated Server")
    await storage.register(server2)
    result2 = await storage.get("server-1")
    assert result2 is not None
    assert result2.created_at is not None
    assert result2.updated_at is not None
    assert result2.created_at == created_at_1
    assert result2.updated_at > updated_at_1


@pytest.mark.asyncio
async def test_jsonfile_storage_unregister_missing_raises_key_error(tmp_path) -> None:
    storage = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage.init()
    with pytest.raises(KeyError) as exc_info:
        await storage.unregister("missing")
    assert exc_info.value.args[0] == "missing"


@pytest.mark.asyncio
async def test_jsonfile_storage_get_returns_deep_copy(tmp_path) -> None:
    storage = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage.init()
    server = RegisteredServer(id="server-1", url="http://localhost:8000", name="Test Server")
    await storage.register(server)
    result = await storage.get("server-1")
    assert result is not None
    original_name = result.name
    result.name = "Mutated Name"
    stored = await storage.get("server-1")
    assert stored is not None
    assert stored.name == original_name


@pytest.mark.asyncio
async def test_inmemory_storage_register_agent_then_get_agent_returns_equal_object() -> None:
    storage = InMemoryStorage()
    await storage.init()
    agent = RegisteredAgent(id="agent-1", url="http://localhost:8001", name="Test Agent")
    await storage.register_agent(agent)
    result = await storage.get_agent("agent-1")
    assert result is not None
    assert result.id == agent.id
    assert result.url == agent.url
    assert result.name == agent.name


@pytest.mark.asyncio
async def test_inmemory_storage_register_agent_twice_preserves_created_at() -> None:
    storage = InMemoryStorage()
    await storage.init()
    agent1 = RegisteredAgent(id="agent-1", url="http://localhost:8001", name="Test Agent")
    await storage.register_agent(agent1)
    result1 = await storage.get_agent("agent-1")
    assert result1 is not None
    created_at_1 = result1.created_at
    updated_at_1 = result1.updated_at
    assert created_at_1 is not None
    assert updated_at_1 is not None

    import asyncio

    await asyncio.sleep(0.01)

    agent2 = RegisteredAgent(id="agent-1", url="http://localhost:8001", name="Updated Agent")
    await storage.register_agent(agent2)
    result2 = await storage.get_agent("agent-1")
    assert result2 is not None
    assert result2.created_at is not None
    assert result2.updated_at is not None
    assert result2.created_at == created_at_1
    assert result2.updated_at > updated_at_1


@pytest.mark.asyncio
async def test_inmemory_storage_get_agent_returns_deep_copy() -> None:
    storage = InMemoryStorage()
    await storage.init()
    agent = RegisteredAgent(id="agent-1", url="http://localhost:8001", name="Test Agent")
    await storage.register_agent(agent)
    result = await storage.get_agent("agent-1")
    assert result is not None
    original_name = result.name
    result.name = "Mutated Name"
    stored = await storage.get_agent("agent-1")
    assert stored is not None
    assert stored.name == original_name


@pytest.mark.asyncio
async def test_inmemory_storage_unregister_agent_missing_raises_key_error() -> None:
    storage = InMemoryStorage()
    await storage.init()
    with pytest.raises(KeyError) as exc_info:
        await storage.unregister_agent("missing")
    assert exc_info.value.args[0] == "missing"


@pytest.mark.asyncio
async def test_inmemory_storage_list_agents() -> None:
    storage = InMemoryStorage()
    await storage.init()
    agent1 = RegisteredAgent(id="agent-1", url="http://localhost:8001", name="Agent 1")
    agent2 = RegisteredAgent(id="agent-2", url="http://localhost:8002", name="Agent 2")
    await storage.register_agent(agent1)
    await storage.register_agent(agent2)
    result = await storage.list_agents()
    assert len(result) == 2
    ids = {a.id for a in result}
    assert ids == {"agent-1", "agent-2"}


@pytest.mark.asyncio
async def test_jsonfile_storage_register_agent_creates_correct_structure(tmp_path) -> None:
    storage = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage.init()
    agent = RegisteredAgent(id="agent-1", url="http://localhost:8001", name="Test Agent")
    await storage.register_agent(agent)

    with open(storage._path, "r") as f:
        data = json.load(f)

    assert "version" in data
    assert "servers" in data
    assert "agents" in data
    assert data["version"] == 1
    assert len(data["agents"]) == 1
    assert data["agents"][0]["id"] == "agent-1"


@pytest.mark.asyncio
async def test_jsonfile_storage_persists_volatile_agent_fields(tmp_path) -> None:
    storage = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage.init()
    agent = RegisteredAgent(
        id="agent-1",
        url="http://localhost:8001",
        name="Test Agent",
        last_card=AgentCard(name="test", description="test", version="1.0", url="http://test"),
        last_card_checked=datetime.now(timezone.utc),
        card_valid=True,
        card_issues=["issue1"],
    )
    await storage.register_agent(agent)

    storage2 = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage2.init()
    result = await storage2.get_agent("agent-1")
    assert result is not None
    assert result.last_card is not None
    assert result.last_card.name == "test"
    assert result.last_card_checked is not None
    assert result.card_valid is True
    assert result.card_issues == ["issue1"]


@pytest.mark.asyncio
async def test_jsonfile_storage_loads_file_without_agents_returns_empty_list(tmp_path) -> None:
    file_path = tmp_path / "mcp_servers.json"
    with open(file_path, "w") as f:
        json.dump({"version": 1, "servers": [{"id": "x", "url": "http://x"}]}, f)

    storage = JSONFileStorage(str(file_path))
    await storage.init()
    agents = await storage.list_agents()
    assert agents == []


@pytest.mark.asyncio
async def test_jsonfile_storage_loads_bare_array_without_agents_returns_empty_list(
    tmp_path,
) -> None:
    file_path = tmp_path / "mcp_servers.json"
    with open(file_path, "w") as f:
        json.dump([{"id": "x", "url": "http://x"}], f)

    storage = JSONFileStorage(str(file_path))
    await storage.init()
    agents = await storage.list_agents()
    assert agents == []


@pytest.mark.asyncio
async def test_jsonfile_storage_agent_round_trip(tmp_path) -> None:
    storage = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage.init()
    agent = RegisteredAgent(id="agent-1", url="http://localhost:8001", name="Test Agent")
    await storage.register_agent(agent)

    result = await storage.get_agent("agent-1")
    assert result is not None
    assert result.id == "agent-1"

    await storage.unregister_agent("agent-1")
    result = await storage.get_agent("agent-1")
    assert result is None


@pytest.mark.asyncio
async def test_jsonfile_storage_register_agent_preserves_created_at(tmp_path) -> None:
    storage = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage.init()
    agent1 = RegisteredAgent(id="agent-1", url="http://localhost:8001", name="Test Agent")
    await storage.register_agent(agent1)
    result1 = await storage.get_agent("agent-1")
    assert result1 is not None
    created_at_1 = result1.created_at
    updated_at_1 = result1.updated_at
    assert created_at_1 is not None
    assert updated_at_1 is not None

    import asyncio

    await asyncio.sleep(0.01)

    agent2 = RegisteredAgent(id="agent-1", url="http://localhost:8001", name="Updated Agent")
    await storage.register_agent(agent2)
    result2 = await storage.get_agent("agent-1")
    assert result2 is not None
    assert result2.created_at is not None
    assert result2.updated_at is not None
    assert result2.created_at == created_at_1
    assert result2.updated_at > updated_at_1


@pytest.mark.asyncio
async def test_jsonfile_storage_unregister_agent_missing_raises_key_error(tmp_path) -> None:
    storage = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage.init()
    with pytest.raises(KeyError) as exc_info:
        await storage.unregister_agent("missing")
    assert exc_info.value.args[0] == "missing"


@pytest.mark.asyncio
async def test_jsonfile_storage_get_agent_returns_deep_copy(tmp_path) -> None:
    storage = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage.init()
    agent = RegisteredAgent(id="agent-1", url="http://localhost:8001", name="Test Agent")
    await storage.register_agent(agent)
    result = await storage.get_agent("agent-1")
    assert result is not None
    original_name = result.name
    result.name = "Mutated Name"
    stored = await storage.get_agent("agent-1")
    assert stored is not None
    assert stored.name == original_name


@pytest.mark.asyncio
async def test_jsonfile_storage_list_agents(tmp_path) -> None:
    storage = JSONFileStorage(str(tmp_path / "mcp_servers.json"))
    await storage.init()
    agent1 = RegisteredAgent(id="agent-1", url="http://localhost:8001", name="Agent 1")
    agent2 = RegisteredAgent(id="agent-2", url="http://localhost:8002", name="Agent 2")
    await storage.register_agent(agent1)
    await storage.register_agent(agent2)
    result = await storage.list_agents()
    assert len(result) == 2
    ids = {a.id for a in result}
    assert ids == {"agent-1", "agent-2"}
