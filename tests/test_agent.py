from datetime import datetime, timezone

import pytest
from mcp_hub.models import AgentCard, RegisteredAgent


class TestAgentCard:
    def test_minimal_card_constructs(self) -> None:
        card = AgentCard(
            name="Test Agent",
            description="A test agent",
            version="1.0.0",
            url="http://example.com",
            capabilities={"streaming": True, "tools": True, "history": True},
        )
        assert card.name == "Test Agent"
        assert card.description == "A test agent"
        assert card.version == "1.0.0"
        assert card.url == "http://example.com"
        assert card.capabilities == {"streaming": True, "tools": True, "history": True}
        assert card.skills == []
        assert card.default_input_modes == []
        assert card.default_output_modes == []
        assert card.auth is None

    def test_full_card_with_all_fields(self) -> None:
        card = AgentCard(
            name="Full Agent",
            description="A fully featured agent",
            version="2.0.0",
            url="http://example.com/agent",
            capabilities={"streaming": True, "tools": False, "history": True},
            skills=[{"id": "skill1", "name": "Skill One", "description": "First skill"}],
            default_input_modes=["text", "json"],
            default_output_modes=["text", "stream"],
            auth={"scheme": "bearer", "token": "secret"},
        )
        assert card.name == "Full Agent"
        assert len(card.skills) == 1
        assert card.skills[0]["id"] == "skill1"
        assert card.default_input_modes == ["text", "json"]
        assert card.default_output_modes == ["text", "stream"]
        assert card.auth == {"scheme": "bearer", "token": "secret"}

    def test_defaults(self) -> None:
        card = AgentCard(
            name="Test",
            description="Test",
            version="1.0",
            url="http://test.com",
            capabilities={},
        )
        assert card.capabilities == {}
        assert card.skills == []
        assert card.default_input_modes == []
        assert card.default_output_modes == []
        assert card.auth is None

    def test_roundtrip_json(self) -> None:
        card = AgentCard(
            name="Test Agent",
            description="A test agent",
            version="1.0.0",
            url="http://example.com",
            capabilities={"streaming": True, "tools": True, "history": True},
        )
        json_str = card.model_dump_json()
        restored = AgentCard.model_validate_json(json_str)
        assert card.name == restored.name
        assert card.description == restored.description
        assert card.version == restored.version
        assert card.url == restored.url
        assert card.capabilities == restored.capabilities


class TestRegisteredAgent:
    def test_required_fields_only(self) -> None:
        agent = RegisteredAgent(id="agent-1", url="http://example.com")
        assert agent.id == "agent-1"
        assert agent.url == "http://example.com"
        assert agent.name == ""
        assert agent.description == ""
        assert agent.tags == []
        assert agent.created_at is None
        assert agent.updated_at is None
        assert agent.bearer_token == ""
        assert agent.last_card is None
        assert agent.last_card_checked is None
        assert agent.card_valid is None
        assert agent.card_issues == []

    def test_full_agent(self) -> None:
        card = AgentCard(
            name="Test Agent",
            description="A test agent",
            version="1.0.0",
            url="http://example.com",
            capabilities={"streaming": True, "tools": True, "history": True},
        )
        agent = RegisteredAgent(
            id="agent-1",
            url="http://example.com",
            name="My Agent",
            description="Agent description",
            tags=["ai", "test"],
            created_at=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            updated_at=datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
            bearer_token="secret-token",
            last_card=card,
            last_card_checked=datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
            card_valid=True,
            card_issues=[],
        )
        assert agent.id == "agent-1"
        assert agent.name == "My Agent"
        assert agent.tags == ["ai", "test"]
        assert agent.bearer_token == "secret-token"
        assert agent.last_card is not None
        assert agent.last_card.name == "Test Agent"
        assert agent.card_valid is True

    def test_sanitize_for_api_removes_bearer_token(self) -> None:
        agent = RegisteredAgent(
            id="agent-1",
            url="http://example.com",
            name="Test Agent",
            bearer_token="secret-token",
        )
        sanitized = agent.sanitize_for_api()
        assert sanitized.bearer_token == ""
        assert sanitized.id == agent.id
        assert sanitized.url == agent.url
        assert sanitized.name == agent.name

    def test_sanitize_for_api_preserves_non_sensitive_fields(self) -> None:
        agent = RegisteredAgent(
            id="agent-1",
            url="http://example.com",
            name="Test Agent",
            description="Description",
            tags=["test"],
            bearer_token="secret-token",
            card_valid=True,
            card_issues=["issue1"],
        )
        sanitized = agent.sanitize_for_api()
        assert sanitized.id == "agent-1"
        assert sanitized.url == "http://example.com"
        assert sanitized.name == "Test Agent"
        assert sanitized.description == "Description"
        assert sanitized.tags == ["test"]
        assert sanitized.card_valid is True
        assert sanitized.card_issues == ["issue1"]

    def test_sanitize_does_not_modify_original(self) -> None:
        agent = RegisteredAgent(
            id="agent-1",
            url="http://example.com",
            bearer_token="secret-token",
        )
        _ = agent.sanitize_for_api()
        assert agent.bearer_token == "secret-token"

    def test_datetime_serializes_to_rfc3339_with_z(self) -> None:
        dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        agent = RegisteredAgent(id="agent-1", url="http://example.com", created_at=dt)
        json_str = agent.model_dump_json()
        assert '"created_at":"2024-01-15T10:30:00Z"' in json_str

    def test_datetime_naive_serializes_to_rfc3339_with_z(self) -> None:
        dt = datetime(2024, 1, 15, 10, 30, 0)
        agent = RegisteredAgent(id="agent-1", url="http://example.com", created_at=dt)
        json_str = agent.model_dump_json()
        assert '"created_at":"2024-01-15T10:30:00Z"' in json_str

    def test_roundtrip_json(self) -> None:
        agent = RegisteredAgent(
            id="agent-1",
            url="http://example.com",
            name="Test Agent",
            description="Description",
            tags=["test"],
        )
        json_str = agent.model_dump_json()
        restored = RegisteredAgent.model_validate_json(json_str)
        assert agent.id == restored.id
        assert agent.url == restored.url
        assert agent.name == restored.name
        assert agent.description == restored.description
        assert agent.tags == restored.tags

    def test_nested_agent_card_roundtrip(self) -> None:
        card = AgentCard(
            name="Test Agent",
            description="A test agent",
            version="1.0.0",
            url="http://example.com",
            capabilities={"streaming": True, "tools": True, "history": True},
        )
        agent = RegisteredAgent(
            id="agent-1",
            url="http://example.com",
            last_card=card,
        )
        json_str = agent.model_dump_json()
        restored = RegisteredAgent.model_validate_json(json_str)
        assert restored.last_card is not None
        assert restored.last_card.name == "Test Agent"
        assert restored.last_card.capabilities == {
            "streaming": True,
            "tools": True,
            "history": True,
        }
