from datetime import datetime

from mcp_hub.models import AgentCard, RegisteredAgent, RegisteredServer
from mcp_hub.storage.base import StorageStrategy
from mcp_hub.utils import utcnow


class Registry:
    def __init__(self, storage: StorageStrategy) -> None:
        self._storage = storage

    async def register(self, server: RegisteredServer) -> RegisteredServer:
        existing = await self._storage.get(server.id)
        if existing is not None and server.created_at is None:
            server.created_at = existing.created_at
        server.updated_at = utcnow()
        if existing is not None:
            if (server.tools is None or len(server.tools) == 0) and existing.tools:
                server.tools = existing.tools
            if (server.prompts is None or len(server.prompts) == 0) and existing.prompts:
                server.prompts = existing.prompts
            if (server.resources is None or len(server.resources) == 0) and existing.resources:
                server.resources = existing.resources
        await self._storage.register(server)
        return server

    async def unregister(self, server_id: str) -> None:
        await self._storage.unregister(server_id)

    async def get(self, server_id: str) -> RegisteredServer | None:
        return await self._storage.get(server_id)

    async def list(self) -> list[RegisteredServer]:
        return await self._storage.list()

    async def update_health(self, server_id: str, *, healthy: bool, consecutive_fails: int) -> None:
        server = await self._storage.get(server_id)
        if server is None:
            raise KeyError(server_id)
        server.healthy = healthy
        server.consecutive_fails = consecutive_fails
        server.last_checked = utcnow()
        await self._storage.save(server)

    async def update_health_and_uptime(
        self,
        server_id: str,
        *,
        healthy: bool,
        consecutive_fails: int,
        uptime: float,
        rate_limited: bool = False,
    ) -> None:
        server = await self._storage.get(server_id)
        if server is None:
            raise KeyError(server_id)
        now = utcnow()
        # Uptime is hub-tracked: measured from when the server most recently became healthy,
        # so it works for any server (the `uptime` argument, a server-reported value from a
        # /health endpoint, is retained for compatibility but no longer the source of truth).
        if healthy:
            if server.healthy_since is None:
                server.healthy_since = now
            server.uptime_seconds = (now - server.healthy_since).total_seconds()
        else:
            server.healthy_since = None
            server.uptime_seconds = 0.0
        server.healthy = healthy
        server.rate_limited = rate_limited
        server.consecutive_fails = consecutive_fails
        server.last_checked = now
        await self._storage.save(server)

    async def set_supports_health_endpoint(self, server_id: str, supports: bool) -> None:
        server = await self._storage.get(server_id)
        if server is None:
            raise KeyError(server_id)
        server.supports_health_endpoint = supports
        await self._storage.save(server)


class AgentRegistry:
    def __init__(self, storage: StorageStrategy) -> None:
        self._storage = storage

    async def register_agent(self, agent: RegisteredAgent) -> RegisteredAgent:
        existing = await self._storage.get_agent(agent.id)
        if existing is not None and agent.created_at is None:
            agent.created_at = existing.created_at
        agent.updated_at = utcnow()
        await self._storage.register_agent(agent)
        return agent

    async def unregister_agent(self, agent_id: str) -> None:
        await self._storage.unregister_agent(agent_id)

    async def get_agent(self, agent_id: str) -> RegisteredAgent | None:
        return await self._storage.get_agent(agent_id)

    async def list_agents(self) -> list[RegisteredAgent]:
        return await self._storage.list_agents()

    async def update_agent_card(
        self,
        agent_id: str,
        *,
        last_card: AgentCard | None,
        last_card_checked: datetime | None,
        card_valid: bool | None,
        card_issues: list[str],
    ) -> None:
        agent = await self._storage.get_agent(agent_id)
        if agent is None:
            raise KeyError(agent_id)
        agent.last_card = last_card
        agent.last_card_checked = last_card_checked
        agent.card_valid = card_valid
        agent.card_issues = card_issues
        agent.updated_at = utcnow()
        await self._storage.save_agent(agent)
