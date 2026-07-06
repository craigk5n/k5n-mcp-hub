import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from mcp_hub.models import RegisteredAgent, RegisteredServer
from mcp_hub.storage.base import StorageStrategy


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JSONFileStorage(StorageStrategy):
    def __init__(self, path: str = "mcp_servers.json") -> None:
        self._path = Path(path)
        self._data: dict[str, RegisteredServer] = {}
        self._agents: dict[str, RegisteredAgent] = {}
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        parent_dir = self._path.parent
        if not parent_dir.exists():
            parent_dir.mkdir(mode=0o700, parents=True)

        if self._path.exists() and self._path.stat().st_size > 0:
            with open(self._path, "r") as f:
                content = json.load(f)

            if isinstance(content, dict) and "servers" in content:
                servers_data = content["servers"]
            elif isinstance(content, list):
                servers_data = content
            else:
                servers_data = []

            for server_data in servers_data:
                server = RegisteredServer.model_validate(server_data)
                self._data[server.id] = server

            agents_data = []
            if isinstance(content, dict) and "agents" in content:
                agents_data = content["agents"]

            for agent_data in agents_data:
                agent = RegisteredAgent.model_validate(agent_data)
                self._agents[agent.id] = agent

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
            await self._persist_locked()

    async def unregister(self, server_id: str) -> None:
        async with self._lock:
            if server_id not in self._data:
                raise KeyError(server_id)
            del self._data[server_id]
            await self._persist_locked()

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
            await self._persist_locked()

    async def unregister_agent(self, agent_id: str) -> None:
        async with self._lock:
            if agent_id not in self._agents:
                raise KeyError(agent_id)
            del self._agents[agent_id]
            await self._persist_locked()

    async def get_agent(self, agent_id: str) -> RegisteredAgent | None:
        async with self._lock:
            stored = self._agents.get(agent_id, None)
            if stored is None:
                return None
            return stored.model_copy(deep=True)

    async def list_agents(self) -> List[RegisteredAgent]:
        async with self._lock:
            return [a.model_copy(deep=True) for a in self._agents.values()]

    async def _persist_locked(self) -> None:
        servers_list = [
            s.sanitize_for_persistence().model_dump(mode="json", exclude_none=False, by_alias=True)
            for s in self._data.values()
        ]
        agents_list = [
            a.sanitize_for_persistence().model_dump(mode="json", exclude_none=False, by_alias=True)
            for a in self._agents.values()
        ]
        payload = {"version": 1, "servers": servers_list, "agents": agents_list}
        json_str = json.dumps(payload, indent=2)

        parent_dir = self._path.parent
        tmp_fd, tmp_path = tempfile.mkstemp(dir=parent_dir)
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(json_str.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
