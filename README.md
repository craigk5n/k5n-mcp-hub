# k5n-mcp-hub

## Overview

k5n-mcp-hub is a registry and management hub for MCP (Model Context Protocol) servers, built with Python and FastAPI. It provides server discovery, health monitoring, request tracing, fault injection, a reverse proxy for MCP calls, and an admin web UI. It is wire-compatible with the original Go implementation it was ported from — routes, headers, and on-disk storage formats are drop-in compatible, so existing clients and integrations work without modification.

## Quick Start

```bash
pip install -e .[dev]
k5n-mcp-hub
```

Then open `http://localhost:8080` in your browser.

## Configuration

Configuration is loaded from `config.yaml` at the repository root. Environment variables can override config values using two patterns:

- **Bare env var** (highest priority): `SERVER_HTTP_PORT` sets the HTTP port directly.
- **Nested prefix**: Variables with the `MCPHUB_` prefix use `__` as a separator for nested keys. For example, `MCPHUB_SERVER__HTTP_PORT` sets `server.http_port`.

## Wire Compatibility

Routes, headers, and storage are drop-in compatible with the original Go implementation. Existing clients, scripts, and integrations designed for that implementation work seamlessly with k5n-mcp-hub without any changes.

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
