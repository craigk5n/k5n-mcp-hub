import pytest
from fastapi.testclient import TestClient

from devhub.app import create_app


def test_index_html_includes_all_cdn_scripts() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/")

    assert response.status_code == 200
    content = response.text

    assert "https://unpkg.com/htmx.org@2.0.2" in content
    assert "https://unpkg.com/hyperscript.org@0.9.12" in content
    assert "https://cdn.jsdelivr.net/npm/js-yaml@4.1.0/dist/js-yaml.min.js" in content
    assert "https://cdn.tailwindcss.com" in content


def test_index_html_hero_text() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/")

    assert response.status_code == 200
    content = response.text

    assert "MCP Development Hub" in content
    assert "Local MCP Service Registry" in content


def test_index_html_htmx_load_trigger() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/")

    assert response.status_code == 200
    content = response.text

    assert 'hx-get="/ui/servers"' in content
    assert 'hx-trigger="load"' in content
    assert 'hx-swap="innerHTML"' in content


def test_index_html_has_main_container() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/")

    assert response.status_code == 200
    content = response.text

    assert "<main" in content


def test_index_html_add_server_form() -> None:
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/")

    assert response.status_code == 200
    content = response.text

    assert 'hx-post="/v1/register"' in content
    assert "Add Server" in content
