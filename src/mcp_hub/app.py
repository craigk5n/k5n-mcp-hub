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

from mcp_hub.agents.card import AgentRegistry as CardAgentRegistry
from mcp_hub.agents.fixtures import FixtureStore
from mcp_hub.auth import Authenticator, build_authenticator, auth_required
from mcp_hub.registry.service import AgentRegistry
from mcp_hub.config import Settings, load_settings
from mcp_hub.logging_setup import configure_logging
from mcp_hub.metrics import Metrics
from mcp_hub.middleware import create_request_id_metrics_middleware
from mcp_hub.trace.recorder import format_headers, sanitize_trace_headers
from mcp_hub.health.checker import HealthChecker
from mcp_hub.mcp.discovery import DiscoveryService
from mcp_hub.registry.service import Registry
from mcp_hub.storage import InMemoryStorage, JSONFileStorage, StorageStrategy
from mcp_hub.trace.recorder import TraceRecorder
from mcp_hub.utils import dom_id

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
    # Initialize the storage backend before serving. For JSONFileStorage this loads any
    # previously persisted servers/agents from disk; for InMemoryStorage it is a no-op.
    storage = getattr(app.state, "storage", None)
    if storage is not None:
        await storage.init()

    # Start the background health checker so servers actually get health-checked (otherwise
    # every server stays "Unknown"/never-checked). Registered as a task so it's cancelled on
    # shutdown. Guarded so a missing dependency can't block startup.
    settings = getattr(app.state, "settings", None)
    if settings is not None and getattr(app.state, "registry", None) is not None:
        health_checker = HealthChecker(
            app.state.registry,
            settings.healthcheck,
            app.state.trace_recorder,
            settings.trace,
            allow_private_networks=settings.security.allow_private_networks,
        )
        register_background_task(app, asyncio.create_task(health_checker.run_forever()))

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
    env.filters["dom_id"] = dom_id
    env.filters["sanitize_headers"] = sanitize_trace_headers
    env.filters["format_headers"] = format_headers

    return env


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = load_settings()

    if settings.storage.type == "redis":
        raise NotImplementedError("redis storage not implemented in v1")

    configure_logging()

    app = FastAPI(
        title="k5n-mcp-hub",
        version="0.1.0",
        lifespan=lifespan,
    )

    metrics = Metrics()

    app.add_middleware(create_request_id_metrics_middleware(metrics))  # type: ignore[arg-type]

    storage: StorageStrategy = (
        JSONFileStorage(settings.storage.json_.path)
        if settings.storage.type == "json"
        else InMemoryStorage()
    )
    if settings.storage.type == "json":
        logger.info(
            "Storage backend: json (persisting to %s)",
            Path(settings.storage.json_.path).resolve(),
        )
    else:
        logger.info(
            "Storage backend: in-memory (servers are NOT persisted across restarts; "
            "set storage.type: json to persist)"
        )
    registry = Registry(storage)
    agent_registry = AgentRegistry(storage)
    card_agent_registry = CardAgentRegistry()
    authenticator: Authenticator = build_authenticator(settings.auth)
    auth_dependency = auth_required(authenticator)
    # Let outbound MCP/discovery connections reach localhost/LAN when the operator opts in
    # (local-first mode). The flag is threaded explicitly into the SSRF-pinned transport via
    # each MCPClient/DiscoveryService — never a process-global — so it can't leak across apps.
    allow_private_networks = settings.security.allow_private_networks
    discovery_service = DiscoveryService(registry, allow_private_networks=allow_private_networks)

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
    from mcp_hub.routes import system
    from mcp_hub.routes import api
    from mcp_hub.routes import mcp
    from mcp_hub.routes import v1
    from mcp_hub.routes import registry_api
    from mcp_hub.routes import proxy
    from mcp_hub.routes import ui_servers
    from mcp_hub.routes import ui_capabilities
    from mcp_hub.routes import ui_trace
    from mcp_hub.routes import ui_playground
    from mcp_hub.routes import ui_agents
    from mcp_hub.routes import ui_initialize
    from mcp_hub.routes import ui_invoke
    from mcp_hub.routes import ui_downloads
    from mcp_hub.routes import ui_faults

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


def _mount_static(app: FastAPI) -> None:
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists() and any(static_dir.iterdir()):
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    else:
        logger.debug("Static directory empty or not present, skipping mount")
