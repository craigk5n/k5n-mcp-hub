from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from devhub.agents.card import AgentRegistry as CardAgentRegistry
from devhub.agents.fixtures import FixtureStore
from devhub.auth import Authenticator, build_authenticator, auth_required
from devhub.registry.service import AgentRegistry
from devhub.config import Settings, load_settings
from devhub.logging_setup import configure_logging
from devhub.metrics import Metrics
from devhub.middleware import create_request_id_metrics_middleware
from devhub.trace.recorder import sanitize_trace_headers
from devhub.mcp.discovery import DiscoveryService
from devhub.registry.service import Registry
from devhub.storage import InMemoryStorage
from devhub.trace.recorder import TraceRecorder

logger = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT_SECONDS = 5


@dataclass
class AppContext:
    background_tasks: list[asyncio.Task[Any]]


@dataclass
class PlaceholderStorage:
    pass


@dataclass
class PlaceholderRegistry:
    pass


@dataclass
class PlaceholderAuthenticator:
    pass


@dataclass
class PlaceholderTraceRecorder:
    pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Application lifespan starting")
    existing_context = getattr(app.state, "context", None)
    if existing_context is None:
        context = AppContext(background_tasks=[])
        app.state.context = context
    else:
        context = existing_context
    try:
        yield
    finally:
        logger.info("Application lifespan shutting down")
        await _cancel_and_await_tasks(context.background_tasks, SHUTDOWN_TIMEOUT_SECONDS)


async def _cancel_and_await_tasks(tasks: list[asyncio.Task[Any]], timeout_seconds: float) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()

    if not tasks:
        return

    await asyncio.wait_for(
        asyncio.gather(*tasks, return_exceptions=True),
        timeout=timeout_seconds,
    )


def register_background_task(app: FastAPI, task: asyncio.Task[Any]) -> None:
    app.state.context.background_tasks.append(task)


def _create_jinja2_environment() -> Environment:
    templates_dir = Path(__file__).parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=False,
        lstrip_blocks=False,
        enable_async=True,
    )

    env.filters["has"] = lambda seq, item: item in seq

    def icon_src(icons: list[dict]) -> str:
        for icon in icons:
            src = icon.get("src", "")
            if src.startswith(("http://", "https://", "data:image/")):
                return src
        return ""

    env.filters["icon_src"] = icon_src

    def schema_summary(variants: list[dict]) -> str:
        labels = []
        for variant in variants:
            label = variant.get("title") or variant.get("type", "")
            if variant.get("enum"):
                label += " (enum)"
            labels.append(label)
        return ", ".join(labels)

    env.filters["schema_summary"] = schema_summary

    def schema_prop_keys(props: dict) -> list:
        return sorted(props.keys())

    env.filters["schema_prop_keys"] = schema_prop_keys

    def pretty_json(s: str) -> str:
        try:
            return json.dumps(json.loads(s), indent=2)
        except (json.JSONDecodeError, TypeError):
            return s

    env.filters["pretty_json"] = pretty_json

    def path_encode(value: str) -> str:
        return quote(value, safe="")

    env.filters["path_encode"] = path_encode
    env.filters["sanitize_headers"] = sanitize_trace_headers

    return env


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = load_settings()

    if settings.storage.type == "redis":
        raise NotImplementedError("redis storage not implemented in v1")

    configure_logging()

    app = FastAPI(
        title="DevHub",
        version="0.1.0",
        lifespan=lifespan,
    )

    metrics = Metrics()

    app.add_middleware(create_request_id_metrics_middleware(metrics))  # type: ignore[arg-type]

    storage = InMemoryStorage()
    registry = Registry(storage)
    agent_registry = AgentRegistry(storage)
    card_agent_registry = CardAgentRegistry()
    authenticator: Authenticator = build_authenticator(settings.auth)
    auth_dependency = auth_required(authenticator)
    discovery_service = DiscoveryService(registry)

    app.state.settings = settings
    app.state.metrics = metrics
    app.state.storage = storage
    app.state.registry = registry
    app.state.agent_registry = agent_registry
    app.state.card_agent_registry = card_agent_registry
    app.state.registry_lock = asyncio.Lock()
    app.state.authenticator = authenticator
    app.state.auth_required_dependency = auth_dependency
    app.state.discovery_service = discovery_service
    trace_recorder = TraceRecorder()
    app.state.trace_recorder = trace_recorder
    app.state.templates = _create_jinja2_environment()
    app.state.fixture_store = FixtureStore()

    _mount_routers(app)
    _mount_static(app)

    return app


def _mount_routers(app: FastAPI) -> None:
    from devhub.routes import system
    from devhub.routes import api
    from devhub.routes import mcp
    from devhub.routes import v1
    from devhub.routes import registry_api
    from devhub.routes import proxy
    from devhub.routes import ui_servers
    from devhub.routes import ui_capabilities
    from devhub.routes import ui_trace
    from devhub.routes import ui_playground
    from devhub.routes import ui_agents
    from devhub.routes import ui_initialize
    from devhub.routes import ui_invoke
    from devhub.routes import ui_downloads
    from devhub.routes import ui_faults
    from devhub.routes import ui_conformance

    app.include_router(system.router)
    app.include_router(registry_api.v1_servers_router)
    app.include_router(registry_api.api_servers_router)
    app.include_router(api.router)
    app.include_router(proxy.router)
    app.include_router(mcp.router)
    app.include_router(v1.router)
    app.include_router(registry_api.router)
    app.include_router(ui_servers.router)
    app.include_router(ui_servers.api_router)
    app.include_router(ui_capabilities.router)
    app.include_router(ui_trace.router)
    app.include_router(ui_playground.router)
    app.include_router(ui_agents.router)
    app.include_router(ui_agents.v1_agents_router)
    app.include_router(ui_initialize.router)
    app.include_router(ui_invoke.router)
    app.include_router(ui_downloads.router)
    app.include_router(ui_faults.router)
    app.include_router(ui_conformance.router)


def _mount_static(app: FastAPI) -> None:
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists() and any(static_dir.iterdir()):
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    else:
        logger.debug("Static directory empty or not present, skipping mount")
