import asyncio
from datetime import datetime, timezone
from typing import List

from mcp_hub.models import RegisteredAgent, RegisteredServer
from mcp_hub.storage.base import StorageStrategy


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryStorage(StorageStrategy):
    def __init__(self) -> None:
        self._data: dict[str, RegisteredServer] = {}
        self._agents: dict[str, RegisteredAgent] = {}
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def register(self, server: RegisteredServer) -> None:
        await self.save(server)

    async def save(self, server: RegisteredServer) -> None:
        async with self._lock:
            if server.id in self._data:
                existing = self._data[server.id]
                server.created_at = existing.created_at
                server.updated_at = _utcnow()
            else:
                server.created_at = _utcnow()
                server.updated_at = _utcnow()
            self._data[server.id] = server

    async def unregister(self, server_id: str) -> None:
        async with self._lock:
            if server_id not in self._data:
                raise KeyError(server_id)
            del self._data[server_id]

    async def get(self, server_id: str) -> RegisteredServer | None:
        async with self._lock:
            stored = self._data.get(server_id, None)
            if stored is None:
                return None
            return stored.model_copy(deep=True)

    async def list(self) -> List[RegisteredServer]:
        async with self._lock:
            return [s.model_copy(deep=True) for s in self._data.values()]

    async def register_agent(self, agent: RegisteredAgent) -> None:
        await self.save_agent(agent)

    async def save_agent(self, agent: RegisteredAgent) -> None:
        async with self._lock:
            if agent.id in self._agents:
                existing = self._agents[agent.id]
                agent.created_at = existing.created_at
                agent.updated_at = _utcnow()
            else:
                agent.created_at = _utcnow()
                agent.updated_at = _utcnow()
            self._agents[agent.id] = agent

    async def unregister_agent(self, agent_id: str) -> None:
        async with self._lock:
            if agent_id not in self._agents:
                raise KeyError(agent_id)
            del self._agents[agent_id]

    async def get_agent(self, agent_id: str) -> RegisteredAgent | None:
        async with self._lock:
            stored = self._agents.get(agent_id, None)
            if stored is None:
                return None
            return stored.model_copy(deep=True)

    async def list_agents(self) -> List[RegisteredAgent]:
        async with self._lock:
            return [a.model_copy(deep=True) for a in self._agents.values()]
