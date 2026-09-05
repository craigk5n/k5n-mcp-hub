# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

k5n-mcp-hub is a gateway and management hub for MCP (Model Context Protocol) servers, built with Python + FastAPI. It was originally built using the agent-dev-team tool.

The **headline differentiator is on-behalf-of**: with `auth.type: jwt` and a server registered `auth_type: obo`, the hub validates the caller's access token and exchanges it (RFC 8693) for one whose audience is the downstream server, so the backend sees the actual user rather than one shared service identity. Design decisions live in `docs/adr/`; a Keycloak-backed demonstration lives in `e2e/`. Around that gateway sits the management plane: discovery, health monitoring, request tracing, fault injection, a reverse proxy, and an admin UI.

> **Treat the public contract as stable.** Before changing any route path, response header, JSON field name, or on-disk storage shape, assume a client depends on it. `tests/test_readme.py` even asserts the README's documented commands stay accurate.

## Naming

The Python import package is `mcp_hub` (`from mcp_hub.app import create_app`). The installed CLI command is `k5n-mcp-hub`. The env-override prefix is `MCPHUB_`. Do not reintroduce the old `devhub`/`dev-hub`/`DEVHUB_` names.

## Commands

Canonical local check sequence (run all before considering work complete):

```bash
python3 -m pip install -e .[dev]   # one-time dev setup
ruff check .
ruff format --check .
mypy --explicit-package-bases --ignore-missing-imports src
python3 -m pytest -v
```

> **Use `python3 -m` for `pip` and `pytest`, not the bare commands.** They can
> resolve to different interpreters — a `~/.local/bin/pytest` with a
> `/usr/bin/python3` shebang sitting next to a pyenv `pip`, for instance — so the
> dev install lands in one Python and the suite runs under another. The failure is
> easy to misread: `pythonpath = ["src", "."]` (`pyproject.toml`) makes `mcp_hub`
> importable straight from the source tree, so all but a handful of tests still
> pass under the wrong interpreter, and the few that need the third-party `mcp`
> SDK fail with a bare `ModuleNotFoundError: No module named 'mcp'` that looks
> like a packaging bug rather than an environment one. `python3 -m` binds both
> commands to the same interpreter; activating the repo's `.venv` works too.

Run the app:

```bash
k5n-mcp-hub                     # serves on http://localhost:8080
k5n-mcp-hub --dev               # also permits localhost/LAN backends (relaxes the SSRF guard)
k5n-mcp-hub --port 9000 --host 0.0.0.0 --config path/to/config.yaml
```

> `--dev` exists because `security.allow_private_networks` defaults to False in
> `config.py` and only the repo's own `config.yaml` turns it on — so an installed CLI
> run outside the repo rejects `http://127.0.0.1/...` with "URL validation failed".
> It sets that one flag (via the documented env override, since uvicorn re-reads
> settings in the app factory) and deliberately nothing else: it never changes
> `auth.type`.

Single test / focused runs:

```bash
python3 -m pytest tests/test_proxy_handler.py
python3 -m pytest tests/test_proxy_handler.py::test_name
python3 -m pytest -k discovery
python3 -m pytest --cov=src --cov-report=term-missing
```

CI (`.github/workflows/ci.yml`) runs the same sequence on a clean runner across Python 3.11 and 3.12, installing with `pip install -e .` so the environment holds **only the declared dependencies**, plus a non-blocking `pip-audit`. If a test passes locally but fails CI, a module is imported but missing from `pyproject.toml` `dependencies` — that clean-install gate is the usual cause.

## Configuration

Loaded from `config.yaml` at repo root. Values there mirror the built-in defaults with one exception: `storage.type` is `json`, so a repo checkout persists its registry to `mcp_servers.json` across restarts instead of losing it. The Pydantic default is still `inmemory`, which is what an installed CLI gets when it finds no `config.yaml`. Two env-override patterns:

- **Bare var** (highest priority): `SERVER_HTTP_PORT=8080`.
- **`MCPHUB_` prefix, `__` as nesting separator**: `MCPHUB_AUTH__BASIC_AUTH__REGISTER_PASS=x` → `auth.basic_auth.register_pass`, `MCPHUB_STORAGE__TYPE=json`.

Settings are Pydantic models in `config.py`. Note the storage-type validator normalizes `json`/`jsonfile`/`file` → `json`; `redis` is accepted by config but raises `NotImplementedError` at app creation. The `json` storage config field is aliased (`json_` ↔ `json`) because `json` is reserved.

## Architecture

**Entry flow:** `k5n-mcp-hub` script → `__main__.py:main` → `uvicorn.run("mcp_hub.app:create_app", factory=True)`. `create_app()` in `app.py` is the composition root — it wires every subsystem into `app.state` (`registry`, `agent_registry`, `storage`, `authenticator`, `trace_recorder`, `metrics`, `discovery_service`, the Jinja2 `templates` env, `fixture_store`) and mounts all routers. Routes read dependencies off `app.state`, not module globals.

**Subsystems** (each is its own package under `src/mcp_hub/`):

- `registry/` — `Registry` (MCP servers) and `AgentRegistry`, thin async services over a storage strategy. Register merges: re-registering a server with empty tools/prompts/resources preserves the previously discovered ones.
- `storage/` — `StorageStrategy` protocol with `InMemoryStorage` (default), a JSON-file backend, plus fixture/memory helpers. Swap backend via config, not code.
- `mcp/` — the MCP protocol layer: `discovery.py` (periodic tool/prompt/resource discovery; probes `server/discover` first, falls back to the `initialize` handshake via `sdk_client.MCPClient`, honors `ttlMs` pacing hints), `stateless.py` (hand-rolled client for the stateless 2026-07-28 revision — the pinned `mcp<2` SDK only speaks handshake revisions; lifting that pin is TODO.md Story 4.1), `jsonrpc.py`, `sse.py`, `oauth.py`, `auth.py` (per-server auth application), `validation.py`, `schema_refs.py`, `constants.py` (protocol versions and per-revision method sets).
- `proxy/` — reverse-proxies MCP calls to registered backends: `handler.py` streams responses (incl. SSE), `url.py` composes backend URLs, `fault_injection.py` injects latency/errors for testing.
- `health/` — background health checker with configurable interval, timeout, failure threshold, and optional auto-unregister.
- `trace/` — `TraceRecorder` captures request/response entries with header sanitization and body truncation (`trace.body_limit`).
- `agents/` — A2A-style agent cards and fixtures (`card.py`, `fixtures.py`); `FixtureStore` reads/writes agent JSON under a repo-root data dir (`.mcp_hub/fixtures/` by default, gitignored — the app creates it at runtime).
- `middleware.py` / `metrics.py` — request-id + Prometheus-style `/metrics`.

**Routing convention:** JSON/API routers (`registry_api.py`, `api.py`, `v1.py`, `mcp.py`, `proxy.py`, `system.py`) carry the public JSON/HTTP API surface. HTML admin-UI routers are the `ui_*.py` files, rendered through the **async** Jinja2 environment built in `app.py` (custom filters: `has`, `icon_src`, `schema_summary`, `pretty_json`, `path_encode`, `sanitize_headers`). Templates live in `templates/`, static assets in `static/`.

## Security notes specific to this codebase

- Outbound MCP/discovery/proxy requests use an **SSRF-pinned transport** (`utils.set_allow_private_networks`). Because this is a *local-first* hub, `config.yaml` ships with `security.allow_private_networks: true` so it can reach localhost/LAN servers. Keep that in mind before "hardening" it — it is intentional for the local use case, but should be false for any untrusted deployment.
- `auth.type` ships as `none` for local convenience; `basic` auth exists for real deployments and needs `register_pass` set.
