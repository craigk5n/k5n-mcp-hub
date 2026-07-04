import re

import httpx
import pytest
from fastapi.testclient import TestClient

from devhub.app import create_app
from devhub.config import Settings, StorageConfig
from devhub.metrics import metrics


def test_create_app_returns_fastapi_with_state() -> None:
    app = create_app()

    assert app.state.settings is not None
    assert app.state.registry is not None
    assert app.state.authenticator is not None
    assert app.state.trace_recorder is not None
    assert app.state.templates is not None


def test_create_app_returns_independent_instances() -> None:
    app1 = create_app()
    app2 = create_app()

    assert app1.state.settings is not app2.state.settings
    assert app1.state.registry is not app2.state.registry
    assert app1.state.authenticator is not app2.state.authenticator
    assert app1.state.trace_recorder is not app2.state.trace_recorder

    app1.state.registry.some_attr = "test_value"
    assert not hasattr(app2.state.registry, "some_attr")


def test_create_app_raises_on_redis_storage() -> None:
    settings = Settings(storage=StorageConfig(type="redis"))

    try:
        create_app(settings)
        assert False, "Expected NotImplementedError"
    except NotImplementedError as e:
        assert str(e) == "redis storage not implemented in v1"


def test_create_app_loads_settings_when_none() -> None:
    app = create_app()

    assert app.state.settings is not None
    assert isinstance(app.state.settings, Settings)


def test_healthz_without_request_id_generates_32_hex_char_id() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/healthz")

    assert response.status_code == 200
    request_id = response.headers.get("X-Request-ID")
    assert request_id is not None
    assert re.match(r"^[0-9a-f]{32}$", request_id) is not None


def test_healthz_with_request_id_echoes_header() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/healthz", headers={"X-Request-ID": "abc-123"})

    assert response.status_code == 200
    assert response.headers.get("X-Request-ID") == "abc-123"


def test_metrics_requests_total_incremented_after_request() -> None:
    app = create_app()
    app.state.metrics.reset()
    client = TestClient(app, raise_server_exceptions=False)

    assert app.state.metrics.requests_total == 0

    client.get("/healthz")

    assert app.state.metrics.requests_total == 1


def test_healthz_returns_plain_text_ok() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.text == "ok"
    assert response.headers.get("content-type") == "text/plain; charset=utf-8"


def test_metrics_returns_plain_text_prometheus() -> None:
    app = create_app()
    app.state.metrics.reset()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers.get("content-type") == "text/plain; charset=utf-8"
    assert (
        re.match(
            r"^devhub_requests_in_flight \d+\ndevhub_requests_total \d+\ndevhub_request_errors_total \d+\ndevhub_request_duration_ms_sum [\d.]+\n$",
            response.text,
        )
        is not None
    )


def test_root_returns_html_200() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers.get("content-type") == "text/html; charset=utf-8"
    assert "<html" in response.text.lower()


def test_unregistered_path_returns_404() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/nonexistent")

    assert response.status_code == 404
