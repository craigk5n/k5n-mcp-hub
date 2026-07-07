# k5n-mcp-hub

<img src="k5n-mcp-hub-icon.svg" alt="k5n-mcp-hub logo" width="96" align="right">

## Overview

k5n-mcp-hub is a registry and management hub for MCP (Model Context Protocol) servers, built with Python and FastAPI. It provides server discovery, health monitoring, request tracing, fault injection, a reverse proxy for MCP calls, and an admin web UI.

## Quick Start

```bash
pip install -e .[dev]
k5n-mcp-hub
```

Then open `http://localhost:8080` in your browser.

## Run with Docker

Build the image locally (a published `k5n/k5n-mcp-hub` image on Docker Hub is planned — see `TODO.md`):

```bash
docker build -t k5n-mcp-hub .
```

Run it a few different ways:

```bash
# Default (local-first, no auth) — open http://localhost:8080
docker run --rm -p 8080:8080 k5n-mcp-hub

# Custom port
docker run --rm -p 9000:9000 -e SERVER_HTTP_PORT=9000 k5n-mcp-hub

# Reach MCP servers running on the host (localhost/LAN), Linux:
docker run --rm --network host k5n-mcp-hub

# Mount your own config
docker run --rm -p 8080:8080 -v "$PWD/config.yaml:/app/config.yaml" k5n-mcp-hub

# Enable basic auth for a shared deployment (password via env, never baked into the image)
docker run --rm -p 8080:8080 \
  -e MCPHUB_AUTH__TYPE=basic \
  -e MCPHUB_AUTH__BASIC_AUTH__REGISTER_PASS=change-me \
  k5n-mcp-hub

# JSON-file storage persisted to a named volume
docker run --rm -p 8080:8080 \
  -e MCPHUB_STORAGE__TYPE=json \
  -e MCPHUB_STORAGE__JSON__PATH=/data/servers.json \
  -v k5n_mcp_hub_data:/data k5n-mcp-hub
```

The image binds `0.0.0.0` inside the container (so a published `-p` port is reachable) and runs as a non-root user. For any internet-exposed deployment, also set `security.allow_private_networks: false` and review the security notes in `AUDIT_local.md`.

## Configuration

Configuration is loaded from `config.yaml` at the repository root. Environment variables can override config values using two patterns:

- **Bare env var** (highest priority): `SERVER_HTTP_PORT` sets the HTTP port directly.
- **Nested prefix**: Variables with the `MCPHUB_` prefix use `__` as a separator for nested keys. For example, `MCPHUB_SERVER__HTTP_PORT` sets `server.http_port`.

## Tests

Run the canonical local check sequence:

```bash
ruff check .
ruff format --check .
mypy --explicit-package-bases --ignore-missing-imports src
pytest -v
```

## License

TBD

## Acknowledgements

k5n-mcp-hub was originally built using the agent-dev-team tool.
