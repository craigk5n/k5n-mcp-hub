from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mcp_hub.agents.card import (
    AgentRegistry,
    fetch_agent_card,
    refresh_agent_card,
    validate_agent_card,
)
from mcp_hub.models import AgentCard, RegisteredAgent


class TestValidateAgentCard:
    def test_valid_minimal_card(self) -> None:
        payload = {
            "name": "a",
            "description": "d",
            "version": "1",
            "url": "http://x",
            "capabilities": {
                "streaming": False,
                "tools": True,
                "history": False,
            },
        }
        card, issues = validate_agent_card(payload)
        assert card is not None
        assert issues == []
        assert isinstance(card, AgentCard)

    def test_missing_description(self) -> None:
        payload = {
            "name": "a",
            "description": "",
            "version": "1",
            "url": "http://x",
            "capabilities": {"streaming": False, "tools": True, "history": False},
        }
        card, issues = validate_agent_card(payload)
        assert card is None
        assert any("description" in issue for issue in issues)

    def test_missing_name(self) -> None:
        payload = {
            "name": "",
            "description": "d",
            "version": "1",
            "url": "http://x",
            "capabilities": {"streaming": False, "tools": True, "history": False},
        }
        card, issues = validate_agent_card(payload)
        assert card is None
        assert any("name" in issue for issue in issues)

    def test_missing_version(self) -> None:
        payload = {
            "name": "a",
            "description": "d",
            "version": "",
            "url": "http://x",
            "capabilities": {"streaming": False, "tools": True, "history": False},
        }
        card, issues = validate_agent_card(payload)
        assert card is None
        assert any("version" in issue for issue in issues)

    def test_missing_url(self) -> None:
        payload = {
            "name": "a",
            "description": "d",
            "version": "1",
            "url": "",
            "capabilities": {"streaming": False, "tools": True, "history": False},
        }
        card, issues = validate_agent_card(payload)
        assert card is None
        assert any("url" in issue for issue in issues)

    def test_capabilities_streaming_not_bool(self) -> None:
        payload = {
            "name": "a",
            "description": "d",
            "version": "1",
            "url": "http://x",
            "capabilities": {"streaming": "yes", "tools": True, "history": False},
        }
        card, issues = validate_agent_card(payload)
        assert any("streaming" in issue and "bool" in issue for issue in issues)

    def test_capabilities_tools_not_bool(self) -> None:
        payload = {
            "name": "a",
            "description": "d",
            "version": "1",
            "url": "http://x",
            "capabilities": {"streaming": False, "tools": "yes", "history": False},
        }
        card, issues = validate_agent_card(payload)
        assert any("tools" in issue and "bool" in issue for issue in issues)

    def test_capabilities_history_not_bool(self) -> None:
        payload = {
            "name": "a",
            "description": "d",
            "version": "1",
            "url": "http://x",
            "capabilities": {"streaming": False, "tools": True, "history": "no"},
        }
        card, issues = validate_agent_card(payload)
        assert any("history" in issue and "bool" in issue for issue in issues)

    def test_missing_capabilities(self) -> None:
        payload = {
            "name": "a",
            "description": "d",
            "version": "1",
            "url": "http://x",
        }
        card, issues = validate_agent_card(payload)
        assert card is None
        assert any("capabilities" in issue for issue in issues)

    def test_valid_full_card(self) -> None:
        payload = {
            "name": "Test Agent",
            "description": "A test agent",
            "version": "1.0.0",
            "url": "http://example.com",
            "capabilities": {
                "streaming": True,
                "tools": True,
                "history": True,
            },
            "skills": [{"id": "skill1", "name": "Skill 1", "description": "First skill"}],
            "default_input_modes": ["text", "json"],
            "default_output_modes": ["text", "stream"],
            "auth": {"scheme": "bearer", "token": "secret"},
        }
        card, issues = validate_agent_card(payload)
        assert card is not None
        assert issues == []
        assert card.name == "Test Agent"
        assert len(card.skills) == 1

    def test_skills_invalid_entry(self) -> None:
        payload = {
            "name": "a",
            "description": "d",
            "version": "1",
            "url": "http://x",
            "capabilities": {"streaming": False, "tools": True, "history": True},
            "skills": [{"id": "skill1", "name": "", "description": "desc"}],
        }
        card, issues = validate_agent_card(payload)
        assert any("name" in issue for issue in issues)

    def test_default_input_modes_invalid(self) -> None:
        payload = {
            "name": "a",
            "description": "d",
            "version": "1",
            "url": "http://x",
            "capabilities": {"streaming": False, "tools": True, "history": True},
            "default_input_modes": [123],
        }
        card, issues = validate_agent_card(payload)
        assert any("default_input_modes" in issue for issue in issues)

    def test_auth_invalid_scheme(self) -> None:
        payload = {
            "name": "a",
            "description": "d",
            "version": "1",
            "url": "http://x",
            "capabilities": {"streaming": False, "tools": True, "history": True},
            "auth": {"scheme": "invalid"},
        }
        card, issues = validate_agent_card(payload)
        assert any("auth.scheme" in issue for issue in issues)

    def test_auth_valid_bearer(self) -> None:
        payload = {
            "name": "a",
            "description": "d",
            "version": "1",
            "url": "http://x",
            "capabilities": {"streaming": False, "tools": True, "history": True},
            "auth": {"scheme": "bearer"},
        }
        card, issues = validate_agent_card(payload)
        assert card is not None
        assert issues == []

    def test_auth_valid_oauth(self) -> None:
        payload = {
            "name": "a",
            "description": "d",
            "version": "1",
            "url": "http://x",
            "capabilities": {"streaming": False, "tools": True, "history": True},
            "auth": {"scheme": "oauth"},
        }
        card, issues = validate_agent_card(payload)
        assert card is not None

    def test_auth_valid_none(self) -> None:
        payload = {
            "name": "a",
            "description": "d",
            "version": "1",
            "url": "http://x",
            "capabilities": {"streaming": False, "tools": True, "history": True},
            "auth": {"scheme": "none"},
        }
        card, issues = validate_agent_card(payload)
        assert card is not None


class TestFetchAgentCard:
    @pytest.mark.asyncio
    async def test_fetch_success(self) -> None:
        agent = RegisteredAgent(id="test", url="http://example.com")

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "name": "Test",
            "description": "Test agent",
            "version": "1.0",
            "url": "http://example.com",
            "capabilities": {"streaming": True, "tools": False, "history": True},
        }

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await fetch_agent_card(agent, client=mock_client)
        assert result["name"] == "Test"
        mock_client.get.assert_called_once()
        mock_client.aclose.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_with_bearer_token(self) -> None:
        agent = RegisteredAgent(id="test", url="http://example.com", bearer_token="secret")

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "name": "Test",
            "description": "Test agent",
            "version": "1.0",
            "url": "http://example.com",
            "capabilities": {"streaming": True, "tools": False, "history": True},
        }

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        await fetch_agent_card(agent, client=mock_client)

        call_kwargs = mock_client.get.call_args[1]
        assert call_kwargs["headers"]["Authorization"] == "Bearer secret"
        assert call_kwargs["headers"]["Accept"] == "application/json"

    @pytest.mark.asyncio
    async def test_fetch_non_2xx_raises(self) -> None:
        agent = RegisteredAgent(id="test", url="http://example.com")

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 404

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        with pytest.raises(RuntimeError, match="HTTP 404"):
            await fetch_agent_card(agent, client=mock_client)

    @pytest.mark.asyncio
    async def test_fetch_invalid_json_raises(self) -> None:
        agent = RegisteredAgent(id="test", url="http://example.com")

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("Invalid JSON")

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        with pytest.raises(RuntimeError, match="Invalid JSON"):
            await fetch_agent_card(agent, client=mock_client)

    @pytest.mark.asyncio
    async def test_fetch_url_trailing_slash(self) -> None:
        agent = RegisteredAgent(id="test", url="http://example.com/")

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "name": "Test",
            "description": "Test agent",
            "version": "1.0",
            "url": "http://example.com",
            "capabilities": {"streaming": True, "tools": False, "history": True},
        }

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        await fetch_agent_card(agent, client=mock_client)

        call_url = mock_client.get.call_args[0][0]
        assert call_url == "http://example.com/.well-known/agent.json"


class TestRefreshAgentCard:
    @pytest.mark.asyncio
    async def test_refresh_success(self) -> None:
        agent = RegisteredAgent(id="test", url="http://example.com")
        registry = AgentRegistry()

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "name": "Test",
            "description": "Test agent",
            "version": "1.0",
            "url": "http://example.com",
            "capabilities": {"streaming": True, "tools": False, "history": True},
        }

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("mcp_hub.agents.card.httpx.AsyncClient", return_value=mock_client):
            await refresh_agent_card(agent, registry)

        assert agent.last_card is not None
        assert agent.last_card.name == "Test"
        assert agent.card_valid is True
        assert agent.card_issues == []
        assert agent.last_card_checked is not None

    @pytest.mark.asyncio
    async def test_refresh_fetch_error_swallowed(self) -> None:
        agent = RegisteredAgent(id="test", url="http://example.com")
        registry = AgentRegistry()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=Exception("Network error"))
        mock_client.aclose = AsyncMock()

        with patch("mcp_hub.agents.card.httpx.AsyncClient", return_value=mock_client):
            await refresh_agent_card(agent, registry)

        assert agent.last_card is None
        assert agent.card_valid is False
        assert "Network error" in agent.card_issues[0]
        assert agent.last_card_checked is not None

    @pytest.mark.asyncio
    async def test_refresh_validation_error_swallowed(self) -> None:
        agent = RegisteredAgent(id="test", url="http://example.com")
        registry = AgentRegistry()

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "name": "Test",
            "description": "Test agent",
            "version": "1.0",
            "url": "http://example.com",
            "capabilities": "invalid",
        }

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("mcp_hub.agents.card.httpx.AsyncClient", return_value=mock_client):
            await refresh_agent_card(agent, registry)

        assert agent.last_card is None
        assert agent.card_valid is False
        assert len(agent.card_issues) > 0
        assert agent.last_card_checked is not None

    @pytest.mark.asyncio
    async def test_refresh_persists_via_registry(self) -> None:
        agent = RegisteredAgent(id="test", url="http://example.com")
        registry = AgentRegistry()

        mock_response = AsyncMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "name": "Test",
            "description": "Test agent",
            "version": "1.0",
            "url": "http://example.com",
            "capabilities": {"streaming": True, "tools": False, "history": True},
        }

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("mcp_hub.agents.card.httpx.AsyncClient", return_value=mock_client):
            await refresh_agent_card(agent, registry)

        stored = await registry.get("test")
        assert stored is not None
        assert stored.last_card is not None
        assert stored.card_valid is True
