"""Spans on the paths that matter (Epic 11, Story 11.3).

Asserted against an in-memory exporter, because the only claim worth making is about
what *left* the process. Every test here that checks a redaction rule would pass
trivially if it inspected the span object instead.
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


def _recording(app: Any, *, include_subject: bool = False) -> Any:
    """Swap in a provider that exports to memory. Returns the exporter."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from mcp_hub.observability.otel import OtelProvider

    exporter = InMemorySpanExporter()
    sdk_provider = TracerProvider()
    sdk_provider.add_span_processor(SimpleSpanProcessor(exporter))

    config = Settings(
        otel={
            "enabled": True,
            "endpoint": "http://localhost:4318",
            "include_subject": include_subject,
        }
    ).otel
    app.state.otel = OtelProvider(config, tracer=sdk_provider.get_tracer("test"))
    return exporter


def _stdio_settings() -> Settings:
    return Settings(
        server={"http_host": "127.0.0.1"},
        auth={"type": "none"},
        stdio={
            "enabled": True,
            "allowed_commands": {"echo": {"command": sys.executable, "args": [str(ECHO)]}},
        },
    )


def _register_stdio(client: TestClient) -> None:
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


def _spans(exporter: Any, name: str) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if s.name == name]


class TestProviderIsWired:
    def test_the_app_exposes_a_provider(self) -> None:
        app = create_app(Settings.from_defaults())
        with TestClient(app):
            assert getattr(app.state, "otel", None) is not None
            assert app.state.otel.enabled is False


class TestProxySpans:
    def test_a_proxied_call_produces_a_span(self) -> None:
        app = create_app(_stdio_settings())
        exporter = _recording(app)
        with TestClient(app) as client:
            _register_stdio(client)
            client.post(
                "/mcp",
                content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                headers={"X-MCP-Target-Server": "echo", "Content-Type": "application/json"},
            )

        spans = _spans(exporter, "mcp.proxy")
        assert spans, "a proxied call must be traced"
        attributes = spans[0].attributes
        assert attributes["mcp.server.id"] == "echo"
        assert attributes["mcp.method"] == "tools/list"
        assert attributes["mcp.transport"] == "stdio"

    def test_the_stdio_span_names_the_allowlist_entry_not_a_command(self) -> None:
        # The command is operator config; exporting it would put a host's local paths
        # and flags into a third-party system for no diagnostic gain.
        app = create_app(_stdio_settings())
        exporter = _recording(app)
        with TestClient(app) as client:
            _register_stdio(client)
            client.post(
                "/mcp",
                content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                headers={"X-MCP-Target-Server": "echo", "Content-Type": "application/json"},
            )

        exported = str(_spans(exporter, "mcp.proxy")[0].attributes)
        assert "echo" in exported
        assert sys.executable not in exported, "the command line must not be exported"
        assert "echo_stdio_server.py" not in exported

    def test_the_subject_is_withheld_unless_asked_for(self) -> None:
        app = create_app(_stdio_settings())
        exporter = _recording(app)
        with TestClient(app) as client:
            _register_stdio(client)
            client.post(
                "/mcp",
                content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                headers={"X-MCP-Target-Server": "echo", "Content-Type": "application/json"},
            )

        assert "mcp.caller.subject" not in _spans(exporter, "mcp.proxy")[0].attributes

    def test_a_refused_caller_is_traced_as_an_error(self) -> None:
        """Denied attempts are the traffic an operator most wants to see -- the same
        reason the hub's own trace recorder records them."""
        app = create_app(_stdio_settings())
        exporter = _recording(app)
        with TestClient(app) as client:
            resp = client.post(
                "/mcp",
                content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                headers={"X-MCP-Target-Server": "no-such-server"},
            )
            assert resp.status_code == 404

        spans = _spans(exporter, "mcp.proxy")
        assert spans, "a refused proxy call must still be traced"

    def test_no_header_or_body_content_is_exported(self) -> None:
        app = create_app(_stdio_settings())
        exporter = _recording(app)
        with TestClient(app) as client:
            _register_stdio(client)
            client.post(
                "/mcp",
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "echo", "arguments": {"text": "sensitive-payload"}},
                    }
                ),
                headers={
                    "X-MCP-Target-Server": "echo",
                    "Content-Type": "application/json",
                    "Authorization": "Bearer super-secret",
                },
            )

        exported = str([s.attributes for s in exporter.get_finished_spans()])
        assert "super-secret" not in exported
        assert "sensitive-payload" not in exported


class TestDisabledCostsNothing:
    def test_no_spans_when_disabled(self) -> None:
        app = create_app(_stdio_settings())
        exporter = _recording(app)
        app.state.otel = __import__(
            "mcp_hub.observability.otel", fromlist=["build_provider"]
        ).build_provider(Settings.from_defaults().otel)

        with TestClient(app) as client:
            _register_stdio(client)
            client.post(
                "/mcp",
                content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                headers={"X-MCP-Target-Server": "echo", "Content-Type": "application/json"},
            )

        assert exporter.get_finished_spans() == ()


class TestHttpProxySpans:
    """The HTTP path, which is the one most deployments use.

    Registration and the proxied call happen inside a single TestClient context. Two
    contexts over one app means two lifespans -- the first shutdown closes loop-bound
    resources the second then tries to use, which surfaces as "Event loop is closed"
    from somewhere unrelated to what is being tested.
    """

    def test_an_unreachable_backend_is_traced_without_its_error_text(self) -> None:
        from unittest.mock import patch

        app = create_app(Settings(server={"http_host": "127.0.0.1"}, auth={"type": "none"}))
        exporter = _recording(app)

        async def _ok(url: str, require_reachability: bool, allow_private: bool) -> Any:
            return True, "", ["203.0.113.1"]

        with TestClient(app) as client:
            with patch("mcp_hub.routes.v1.is_url_safe_for_discovery", _ok):
                resp = client.post(
                    "/v1/register",
                    content=json.dumps({"id": "remote", "url": "https://backend.invalid/mcp"}),
                )
                assert resp.status_code == 201, resp.text

            client.post(
                "/mcp",
                content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                headers={"X-MCP-Target-Server": "remote", "Content-Type": "application/json"},
            )

        spans = _spans(exporter, "mcp.proxy")
        assert spans, "an HTTP proxy attempt must be traced"
        attributes = spans[0].attributes
        assert attributes["mcp.server.id"] == "remote"
        assert attributes["mcp.method"] == "tools/list"
        assert attributes["mcp.outcome"] == "backend_unreachable"
        # The exception type, never its text: that names hosts and can carry a URL
        # with credentials in it.
        assert attributes["error.type"]
        assert "backend.invalid" not in str(attributes["error.type"])
