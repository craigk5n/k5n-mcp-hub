import asyncio
from datetime import datetime, timezone

import pytest

from devhub.models import AgentCard, RegisteredAgent
from devhub.registry import AgentRegistry
from devhub.storage.memory import InMemoryStorage


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def agent_registry(storage: InMemoryStorage) -> AgentRegistry:
    return AgentRegistry(storage)


@pytest.mark.asyncio
async def test_register_agent_round_trip(agent_registry: AgentRegistry) -> None:
    agent = RegisteredAgent(id="agent1", url="http://localhost:8000", name="Test Agent")
    result = await agent_registry.register_agent(agent)

    assert result.id == "agent1"
    assert result.created_at is not None
    assert result.updated_at is not None

    fetched = await agent_registry.get_agent("agent1")
    assert fetched is not None
    assert fetched.id == "agent1"
    assert fetched.name == "Test Agent"


@pytest.mark.asyncio
async def test_list_agents(agent_registry: AgentRegistry) -> None:
    agent1 = RegisteredAgent(id="agent1", url="http://localhost:8000", name="Agent 1")
    agent2 = RegisteredAgent(id="agent2", url="http://localhost:8001", name="Agent 2")

    await agent_registry.register_agent(agent1)
    await agent_registry.register_agent(agent2)

    agents = await agent_registry.list_agents()
    assert len(agents) == 2
    assert {a.id for a in agents} == {"agent1", "agent2"}


@pytest.mark.asyncio
async def test_unregister_agent(agent_registry: AgentRegistry) -> None:
    agent = RegisteredAgent(id="agent1", url="http://localhost:8000", name="Test Agent")
    await agent_registry.register_agent(agent)

    await agent_registry.unregister_agent("agent1")

    fetched = await agent_registry.get_agent("agent1")
    assert fetched is None


@pytest.mark.asyncio
async def test_register_agent_twice_preserves_created_at(agent_registry: AgentRegistry) -> None:
    agent = RegisteredAgent(id="agent1", url="http://localhost:8000", name="Test")
    result1 = await agent_registry.register_agent(agent)
    created_at_1 = result1.created_at
    updated_at_1 = result1.updated_at

    assert created_at_1 is not None
    assert updated_at_1 is not None

    await asyncio.sleep(0.01)

    agent2 = RegisteredAgent(id="agent1", url="http://localhost:8000", name="Test")
    result2 = await agent_registry.register_agent(agent2)

    assert result2.created_at == created_at_1
    assert result2.updated_at is not None
    assert result2.updated_at > updated_at_1


@pytest.mark.asyncio
async def test_update_agent_card(agent_registry: AgentRegistry) -> None:
    agent = RegisteredAgent(id="agent1", url="http://localhost:8000", name="Test Agent")
    await agent_registry.register_agent(agent)

    card = AgentCard(
        name="Test Agent",
        description="A test agent",
        version="1.0.0",
        url="http://localhost:8000",
    )

    await agent_registry.update_agent_card(
        "agent1",
        last_card=card,
        last_card_checked=datetime.now(timezone.utc),
        card_valid=True,
        card_issues=[],
    )

    updated = await agent_registry.get_agent("agent1")
    assert updated is not None
    assert updated.last_card is not None
    assert updated.last_card.name == "Test Agent"
    assert updated.card_valid is True
    assert updated.card_issues == []


@pytest.mark.asyncio
async def test_update_agent_card_with_issues(agent_registry: AgentRegistry) -> None:
    agent = RegisteredAgent(id="agent1", url="http://localhost:8000", name="Test Agent")
    await agent_registry.register_agent(agent)

    await agent_registry.update_agent_card(
        "agent1",
        last_card=None,
        last_card_checked=None,
        card_valid=False,
        card_issues=["Invalid capability format", "Missing required fields"],
    )

    updated = await agent_registry.get_agent("agent1")
    assert updated is not None
    assert updated.card_valid is False
    assert len(updated.card_issues) == 2
    assert "Invalid capability format" in updated.card_issues


@pytest.mark.asyncio
async def test_update_agent_card_not_found(agent_registry: AgentRegistry) -> None:
    with pytest.raises(KeyError):
        await agent_registry.update_agent_card(
            "nonexistent",
            last_card=None,
            last_card_checked=None,
            card_valid=None,
            card_issues=[],
        )


@pytest.mark.asyncio
async def test_unregister_nonexistent_agent(agent_registry: AgentRegistry) -> None:
    with pytest.raises(KeyError):
        await agent_registry.unregister_agent("nonexistent")
