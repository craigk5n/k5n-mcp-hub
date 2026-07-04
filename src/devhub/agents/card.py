from typing import Any

import httpx

from devhub.models.agent import AgentCard
from devhub.models import RegisteredAgent
from devhub.utils import utcnow


async def fetch_agent_card(
    agent: RegisteredAgent, *, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    url = f"{agent.url.rstrip('/')}/.well-known/agent.json"
    headers: dict[str, str] = {"Accept": "application/json"}
    if agent.bearer_token:
        headers["Authorization"] = f"Bearer {agent.bearer_token}"

    should_close = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)

    try:
        response = await client.get(url, headers=headers)
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"Failed to fetch agent card: HTTP {response.status_code}")
        try:
            return response.json()
        except Exception as e:
            raise RuntimeError(f"Invalid JSON response: {e}")
    finally:
        if should_close:
            await client.aclose()


def validate_agent_card(
    payload: dict[str, Any],
) -> tuple[AgentCard | None, list[str]]:
    issues: list[str] = []
    critical_issues: list[str] = []

    required_fields = ["name", "description", "version", "url"]
    for field in required_fields:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            critical_issues.append(f"Missing or empty required field: {field}")

    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict):
        critical_issues.append("capabilities must be a dict")
    else:
        required_caps = ["streaming", "tools", "history"]
        for cap in required_caps:
            if cap not in capabilities:
                issues.append(f"capabilities missing required field: {cap}")
            elif not isinstance(capabilities[cap], bool):
                issues.append(f"capabilities.{cap} must be a bool")

    if "skills" in payload:
        if not isinstance(payload["skills"], list):
            issues.append("skills must be a list")
        else:
            for i, skill in enumerate(payload["skills"]):
                if not isinstance(skill, dict):
                    issues.append(f"skills[{i}] must be a dict")
                else:
                    for field in ["id", "name", "description"]:
                        value = skill.get(field)
                        if not isinstance(value, str) or not value:
                            issues.append(f"skills[{i}].{field} must be a non-empty string")

    if "default_input_modes" in payload:
        if not isinstance(payload["default_input_modes"], list):
            issues.append("default_input_modes must be a list")
        else:
            for i, mode in enumerate(payload["default_input_modes"]):
                if not isinstance(mode, str) or not mode:
                    issues.append(f"default_input_modes[{i}] must be a non-empty string")

    if "default_output_modes" in payload:
        if not isinstance(payload["default_output_modes"], list):
            issues.append("default_output_modes must be a list")
        else:
            for i, mode in enumerate(payload["default_output_modes"]):
                if not isinstance(mode, str) or not mode:
                    issues.append(f"default_output_modes[{i}] must be a non-empty string")

    auth = payload.get("auth")
    if auth is not None:
        if not isinstance(auth, dict):
            issues.append("auth must be a dict")
        else:
            scheme = auth.get("scheme")
            valid_schemes = ["bearer", "oauth", "none"]
            if scheme not in valid_schemes:
                issues.append(f"auth.scheme must be one of {valid_schemes}, got: {scheme}")

    if critical_issues:
        return None, critical_issues

    all_issues = critical_issues + issues
    if issues:
        try:
            card = AgentCard.model_validate(payload)
            return card, issues
        except Exception as e:
            all_issues.append(f"Failed to construct AgentCard: {e}")
            return None, all_issues

    try:
        card = AgentCard.model_validate(payload)
        return card, []
    except Exception as e:
        all_issues.append(f"Failed to construct AgentCard: {e}")
        return None, all_issues


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, RegisteredAgent] = {}

    async def update_agent_card(self, agent: RegisteredAgent) -> None:
        self._agents[agent.id] = agent

    async def get(self, agent_id: str) -> RegisteredAgent | None:
        return self._agents.get(agent_id)


async def refresh_agent_card(agent: RegisteredAgent, registry: AgentRegistry) -> None:
    try:
        payload = await fetch_agent_card(agent)
        card, issues = validate_agent_card(payload)
        now = utcnow()
        agent.last_card = card
        agent.last_card_checked = now
        agent.card_valid = card is not None and len(issues) == 0
        agent.card_issues = issues
    except Exception as e:
        now = utcnow()
        agent.last_card = None
        agent.last_card_checked = now
        agent.card_valid = False
        agent.card_issues = [str(e)]

    await registry.update_agent_card(agent)


def compare_card_to_expected(actual: AgentCard, expected: dict[str, Any]) -> list[str]:
    drift: list[str] = []
    for key, expected_value in expected.items():
        actual_value = getattr(actual, key, None)
        if actual_value != expected_value:
            drift.append(f"drift: {key} expected={expected_value!r} actual={actual_value!r}")
    return drift
