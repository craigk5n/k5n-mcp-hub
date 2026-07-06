import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from mcp_hub.agents.card import refresh_agent_card
from mcp_hub.agents.card import AgentRegistry as CardAgentRegistry
from mcp_hub.registry.service import AgentRegistry as StorageAgentRegistry
from mcp_hub.agents.fixtures import FixtureStore
from mcp_hub.models import RegisteredAgent
from mcp_hub.utils import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/agent", tags=["ui"])
v1_agents_router = APIRouter(prefix="/v1/agents", tags=["v1"])


def get_agent_registry(request: Request) -> StorageAgentRegistry:
    return request.app.state.agent_registry


def get_card_agent_registry(request: Request) -> CardAgentRegistry:
    return request.app.state.card_agent_registry


async def auth_dependency(request: Request) -> None:
    auth_required_dep = request.app.state.auth_required_dependency
    await auth_required_dep(request)


class AgentRegisterRequest(BaseModel):
    id: str
    url: str
    name: str = ""
    description: str = ""
    tags: list[str] = []
    bearer_token: str = ""


class FixturesResponse(BaseModel):
    fixtures: list[str]


class FixtureBodyResponse(BaseModel):
    body: str


@router.get("/{agent_id}/fixtures", response_model=FixturesResponse)
async def list_fixtures(request: Request, agent_id: str) -> FixturesResponse:
    fixture_store: FixtureStore = request.app.state.fixture_store
    fixtures = await fixture_store.list(agent_id)
    return FixturesResponse(fixtures=fixtures)


@router.post("/{agent_id}/fixtures", status_code=204)
async def create_fixture(request: Request, agent_id: str) -> None:
    fixture_store: FixtureStore = request.app.state.fixture_store
    form = await request.form()
    name = form.get("name")
    body = form.get("body")
    if name is None or body is None:
        raise HTTPException(status_code=400, detail="Missing name or body field")
    if not isinstance(name, str) or not isinstance(body, str):
        raise HTTPException(status_code=400, detail="name and body must be strings")
    body_json = {"content": body}
    await fixture_store.save(agent_id, name, body_json)


@router.delete("/{agent_id}/fixtures/{name}", status_code=204)
async def delete_fixture(request: Request, agent_id: str, name: str) -> None:
    fixture_store: FixtureStore = request.app.state.fixture_store
    try:
        await fixture_store.delete(agent_id, name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Fixture not found")


@router.get("/{agent_id}/fixtures/{name}", response_model=FixtureBodyResponse)
async def get_fixture(request: Request, agent_id: str, name: str) -> FixtureBodyResponse:
    fixture_store: FixtureStore = request.app.state.fixture_store
    try:
        body = await fixture_store.load(agent_id, name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Fixture not found")
    return FixtureBodyResponse(body=body.get("content", ""))


@v1_agents_router.post(
    "/register",
    response_model=None,
    status_code=201,
)
async def register_agent(
    request: Request,
    agent_registry: StorageAgentRegistry = Depends(get_agent_registry),
    card_agent_registry: CardAgentRegistry = Depends(get_card_agent_registry),
    _: None = Depends(auth_dependency),
) -> JSONResponse:
    try:
        return await _register_agent_impl(request, agent_registry, card_agent_registry)
    except Exception:
        logger.exception("Unexpected error in register_agent")
        raise


async def _register_agent_impl(
    request: Request,
    agent_registry: StorageAgentRegistry,
    card_agent_registry: CardAgentRegistry,
) -> JSONResponse:
    body = await request.body()
    if len(body) > 1024 * 1024:
        return JSONResponse({"error": "request body too large"}, status_code=400)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if not isinstance(data, dict):
        return JSONResponse({"error": "id and url required"}, status_code=400)

    try:
        validated = AgentRegisterRequest.model_validate(data)
    except ValidationError as e:
        errors = e.errors()
        if not errors:
            return JSONResponse({"error": "validation error"}, status_code=400)

        for error in errors:
            loc = error.get("loc", [])
            err_type = error.get("type", "")
            msg = error.get("msg", "")
            if ("id" in loc or "url" in loc) and (err_type == "missing" or "is required" in msg):
                return JSONResponse({"error": "id and url required"}, status_code=400)

        first_error = errors[0]
        field = ".".join(str(loc) for loc in first_error.get("loc", []))
        msg = first_error.get("msg", "invalid value")
        return JSONResponse({"error": f"{field}: {msg}"}, status_code=400)

    agent = RegisteredAgent(
        id=validated.id,
        url=validated.url,
        name=validated.name,
        description=validated.description,
        tags=validated.tags,
        bearer_token=validated.bearer_token,
        created_at=utcnow(),
        updated_at=utcnow(),
    )

    registered = await agent_registry.register_agent(agent)

    try:
        await refresh_agent_card(registered, card_agent_registry)
    except Exception:
        logger.warning(
            "Agent card refresh failed for %s, registration still successful",
            registered.id,
        )

    return JSONResponse(
        status_code=201,
        content=registered.sanitize_for_api().model_dump(mode="json"),
    )
