"""Proxying a call through to a stdio server (Epic 9, Story 9.5).

A client sends the hub the same JSON-RPC it would send any MCP server, with
`X-MCP-Target-Server` naming the backend. Whether that backend is a URL or a
subprocess is the hub's problem, not the client's -- which is the whole point of
putting stdio behind the gateway.
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


def _settings() -> Settings:
    return Settings(
        server={"http_host": "127.0.0.1"},  # loopback satisfies the ADR 0007 gate
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


def _call(client: TestClient, payload: dict[str, Any]) -> Any:
    return client.post(
        "/mcp",
        content=json.dumps(payload),
        headers={"X-MCP-Target-Server": "echo", "Content-Type": "application/json"},
    )


class TestProxyToStdio:
    def test_tools_list_through_the_proxy(self) -> None:
        app = create_app(_settings())
        with TestClient(app) as client:
            _register(client)
            resp = _call(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == 1
        assert sorted(t["name"] for t in body["result"]["tools"]) == ["count_chars", "echo"]

    def test_tools_call_through_the_proxy(self) -> None:
        app = create_app(_settings())
        with TestClient(app) as client:
            _register(client)
            resp = _call(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"text": "through the hub"}},
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == 2
        assert "through the hub" in json.dumps(body["result"])

    def test_an_error_from_the_server_becomes_a_jsonrpc_error(self) -> None:
        """A tool that does not exist is the server's answer, not a hub failure: the
        client must get a JSON-RPC error it can read, not a 502."""
        app = create_app(_settings())
        with TestClient(app) as client:
            _register(client)
            resp = _call(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "no_such_tool", "arguments": {}},
                },
            )

        body = resp.json()
        assert body["id"] == 3
        assert "error" in body or body.get("result", {}).get("isError") is True

    def test_the_call_is_traced(self) -> None:
        # Read the recorder directly: traces are exposed through the admin UI as HTML,
        # not as a JSON API, and this is about what gets recorded rather than rendered.
        app = create_app(_settings())
        with TestClient(app) as client:
            _register(client)
            _call(client, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
            entries = app.state.trace_recorder.list("echo")

        proxied = [e for e in entries if e.operation == "proxy"]
        assert proxied, "a proxied stdio call must appear in the trace like any other"
        assert proxied[-1].outbound_url == "stdio:echo"
        assert proxied[-1].status == 200
