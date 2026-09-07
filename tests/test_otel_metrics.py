"""OTel metrics alongside `/metrics` (Epic 11, Story 11.4).

The point is the attribute. `/metrics` has four global counters, so it can say the
hub served 400 requests and 12 failed but not *which server* failed them -- which is
the only question worth asking when something is wrong. These carry `mcp.server.id`.

`/metrics` itself is a documented contract and must not move.
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


def _recording_metrics(app: Any) -> Any:
    """Swap in a provider whose metrics land in memory. Returns a reader."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from mcp_hub.observability.otel import OtelProvider

    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    config = Settings(otel={"enabled": True, "endpoint": "http://localhost:4318"}).otel
    app.state.otel = OtelProvider(config, tracer=object(), meter=meter_provider.get_meter("test"))
    return reader


def _points(reader: Any, name: str) -> list[Any]:
    points: list[Any] = []
    data = reader.get_metrics_data()
    if data is None:
        # None, not an empty structure, when no instrument was ever created -- which
        # is exactly the disabled case.
        return points
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name == name:
                    points.extend(metric.data.data_points)
    return points


def _stdio_settings() -> Settings:
    return Settings(
        server={"http_host": "127.0.0.1"},
        auth={"type": "none"},
        stdio={
            "enabled": True,
            "allowed_commands": {"echo": {"command": sys.executable, "args": [str(ECHO)]}},
        },
    )


def _call(client: TestClient, method: str = "tools/list") -> Any:
    return client.post(
        "/mcp",
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method}),
        headers={"X-MCP-Target-Server": "echo", "Content-Type": "application/json"},
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


class TestProxyMetrics:
    def test_requests_are_counted_per_server(self) -> None:
        app = create_app(_stdio_settings())
        reader = _recording_metrics(app)
        with TestClient(app) as client:
            _register(client)
            _call(client)
            _call(client)

        points = _points(reader, "mcp.hub.proxy.requests")
        assert points, "proxied calls must be counted"
        assert dict(points[0].attributes)["mcp.server.id"] == "echo"
        assert sum(p.value for p in points) == 2

    def test_duration_is_recorded_per_server(self) -> None:
        app = create_app(_stdio_settings())
        reader = _recording_metrics(app)
        with TestClient(app) as client:
            _register(client)
            _call(client)

        points = _points(reader, "mcp.hub.proxy.duration")
        assert points, "duration must be recorded"
        assert dict(points[0].attributes)["mcp.server.id"] == "echo"
        assert points[0].count == 1
        assert points[0].sum >= 0

    def test_failures_are_counted_separately(self) -> None:
        app = create_app(_stdio_settings())
        reader = _recording_metrics(app)
        with TestClient(app) as client:
            resp = client.post(
                "/mcp",
                content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                headers={"X-MCP-Target-Server": "missing"},
            )
            assert resp.status_code == 404

        points = _points(reader, "mcp.hub.proxy.errors")
        assert points, "a refused call must be counted as an error"
        assert dict(points[0].attributes)["mcp.server.id"] == "missing"

    def test_no_credentials_in_metric_attributes(self) -> None:
        app = create_app(_stdio_settings())
        reader = _recording_metrics(app)
        with TestClient(app) as client:
            _register(client)
            client.post(
                "/mcp",
                content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                headers={
                    "X-MCP-Target-Server": "echo",
                    "Authorization": "Bearer super-secret",
                    "Content-Type": "application/json",
                },
            )

        exported = str([dict(p.attributes) for p in _points(reader, "mcp.hub.proxy.requests")])
        assert "super-secret" not in exported

    def test_attributes_stay_low_cardinality(self) -> None:
        """Metric attributes are not span attributes: every distinct value is a new
        time series. The subject is deliberately never a metric attribute, even when
        include_subject is on for traces."""
        app = create_app(_stdio_settings())
        reader = _recording_metrics(app)
        with TestClient(app) as client:
            _register(client)
            _call(client)

        attributes = dict(_points(reader, "mcp.hub.proxy.requests")[0].attributes)
        assert set(attributes) <= {"mcp.server.id", "mcp.transport", "mcp.outcome"}


class TestPrometheusEndpointIsUnchanged:
    def test_it_emits_exactly_the_documented_lines(self) -> None:
        app = create_app(_stdio_settings())
        _recording_metrics(app)
        with TestClient(app) as client:
            body = client.get("/metrics").text

        for line in (
            "mcp_hub_requests_in_flight",
            "mcp_hub_requests_total",
            "mcp_hub_request_errors_total",
            "mcp_hub_request_duration_ms_sum",
        ):
            assert line in body, f"{line} is a documented contract"
        # Nothing OpenTelemetry-shaped leaked into the Prometheus text.
        assert "mcp.hub." not in body


class TestDisabled:
    def test_no_metrics_are_recorded_when_disabled(self) -> None:
        from mcp_hub.observability.otel import build_provider

        app = create_app(_stdio_settings())
        reader = _recording_metrics(app)
        app.state.otel = build_provider(Settings.from_defaults().otel)

        with TestClient(app) as client:
            _register(client)
            _call(client)

        assert _points(reader, "mcp.hub.proxy.requests") == []
