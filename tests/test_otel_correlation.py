"""Correlating a span with the hub's own trace view (Epic 11, Story 11.5).

The hub records request/response bodies for an admin; a collector records timings and
outcomes. Both describe the same request. Without a shared identifier an operator who
finds a slow call in one has no way to find it in the other, which makes the pair
worth much less than either alone.

`X-Request-ID` is the identifier the hub already has: `middleware.py` reads it from
the request or generates one, and echoes it on the response.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcp_hub.app import create_app
from mcp_hub.config import Settings

ECHO = Path(__file__).parent / "fixtures" / "echo_stdio_server.py"

pytest.importorskip("opentelemetry.sdk")


def _recording(app: Any) -> Any:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from mcp_hub.observability.otel import OtelProvider

    exporter = InMemorySpanExporter()
    sdk = TracerProvider()
    sdk.add_span_processor(SimpleSpanProcessor(exporter))
    config = Settings(otel={"enabled": True, "endpoint": "http://localhost:4318"}).otel
    app.state.otel = OtelProvider(config, tracer=sdk.get_tracer("test"))
    return exporter


def _settings() -> Settings:
    return Settings(
        server={"http_host": "127.0.0.1"},
        auth={"type": "none"},
        stdio={
            "enabled": True,
            "allowed_commands": {"echo": {"command": sys.executable, "args": [str(ECHO)]}},
        },
    )


def _register(client: TestClient) -> None:
    resp = client.post(
        "/v1/register",
        content=json.dumps(
            {
                "id": "echo",
                "transport_kind": "stdio",
                "stdio_command_name": "echo",
                "registration_type": "manual",
            }
        ),
    )
    assert resp.status_code == 201, resp.text


def _proxy_spans(exporter: Any) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if s.name == "mcp.proxy"]


class TestRequestIdCorrelation:
    def test_a_caller_supplied_request_id_reaches_the_span(self) -> None:
        app = create_app(_settings())
        exporter = _recording(app)
        with TestClient(app) as client:
            _register(client)
            resp = client.post(
                "/mcp",
                content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                headers={
                    "X-MCP-Target-Server": "echo",
                    "Content-Type": "application/json",
                    "X-Request-ID": "corr-12345",
                },
            )

        assert resp.headers["X-Request-ID"] == "corr-12345"
        assert _proxy_spans(exporter)[0].attributes["mcp.request.id"] == "corr-12345"

    def test_a_generated_request_id_is_the_same_one_the_client_is_told(self) -> None:
        """The generated id is only useful if the value on the span is the value the
        caller saw -- otherwise there are two ids for one request and neither joins."""
        app = create_app(_settings())
        exporter = _recording(app)
        with TestClient(app) as client:
            _register(client)
            resp = client.post(
                "/mcp",
                content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                headers={"X-MCP-Target-Server": "echo", "Content-Type": "application/json"},
            )

        returned = resp.headers["X-Request-ID"]
        assert returned
        assert _proxy_spans(exporter)[0].attributes["mcp.request.id"] == returned

    def test_the_request_id_is_not_treated_as_a_credential(self) -> None:
        # `mcp.request.id` contains "id" but nothing secret; the attribute filter must
        # not eat it. Guarding against over-broad filtering as much as under-broad.
        app = create_app(_settings())
        exporter = _recording(app)
        with TestClient(app) as client:
            _register(client)
            client.post(
                "/mcp",
                content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                headers={
                    "X-MCP-Target-Server": "echo",
                    "Content-Type": "application/json",
                    "X-Request-ID": "corr-abc",
                },
            )

        assert "mcp.request.id" in _proxy_spans(exporter)[0].attributes
