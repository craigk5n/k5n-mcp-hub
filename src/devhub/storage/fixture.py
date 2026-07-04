import asyncio


class FixtureStore:
    def __init__(self) -> None:
        self._fixtures: dict[str, dict[str, str]] = {}
        self._lock = asyncio.Lock()

    async def list_fixtures(self, agent_id: str) -> list[str]:
        async with self._lock:
            agent_fixtures = self._fixtures.get(agent_id, {})
            return sorted(agent_fixtures.keys())

    async def get_fixture(self, agent_id: str, name: str) -> str | None:
        async with self._lock:
            agent_fixtures = self._fixtures.get(agent_id, {})
            return agent_fixtures.get(name)

    async def save_fixture(self, agent_id: str, name: str, body: str) -> None:
        async with self._lock:
            if agent_id not in self._fixtures:
                self._fixtures[agent_id] = {}
            self._fixtures[agent_id][name] = body

    async def delete_fixture(self, agent_id: str, name: str) -> None:
        async with self._lock:
            if agent_id not in self._fixtures:
                raise KeyError(f"Fixture {name} not found for agent {agent_id}")
            if name not in self._fixtures[agent_id]:
                raise KeyError(f"Fixture {name} not found for agent {agent_id}")
            del self._fixtures[agent_id][name]
