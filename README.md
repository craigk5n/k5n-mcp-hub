# MCP Dev Hub (Python Re-implementation)

## Overview

Dev-hub is a Python re-implementation of the Go-based MCP dev-hub, providing full wire-compatibility with the original. It serves as a registry and management hub for MCP (Model Context Protocol) servers, enabling server discovery, health monitoring, and proxy functionality. The Python implementation maintains API compatibility with the Go version, allowing existing clients and integrations to work without modification.

## Quick Start

```bash
pip install -e .[dev]
devhub
```

Then open `http://localhost:8080` in your browser.

## Configuration

Configuration is loaded from `config.yaml` at the repository root. Environment variables can override config values using two patterns:

- **Bare env var** (highest priority): `SERVER_HTTP_PORT` sets the HTTP port directly.
- **Nested prefix**: Variables with the `DEVHUB_` prefix use `__` as a separator for nested keys. For example, `DEVHUB_SERVER__HTTP_PORT` sets `server.http_port`.

## Wire Compatibility

Routes, headers, and storage are drop-in compatible with the Go dev-hub. This means existing clients, scripts, and integrations designed for the Go implementation will work seamlessly with this Python re-implementation without any changes.

## Tests

Run the canonical local check sequence:

```bash
ruff check .
ruff format --check .
mypy devhub/ --strict
pytest -v
```

## License

TBD
