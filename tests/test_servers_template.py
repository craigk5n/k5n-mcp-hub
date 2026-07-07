import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path


def _create_test_environment() -> Environment:
    templates_dir = Path(__file__).parent.parent / "src" / "mcp_hub" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return env


def _create_sample_servers() -> list[dict]:
    return [
        {
            "id": "test-server-1",
            "url": "http://localhost:8001/mcp",
            "name": "Test Server One",
            "version": "1.0.0",
            "description": "A test server for unit testing",
            "tags": ["testing", "demo"],
            "registration_type": "self",
        },
        {
            "id": "test-server-2",
            "url": "http://localhost:8002/mcp",
            "name": "Test Server Two",
            "version": "2.1.3",
            "description": "Another test server",
            "tags": ["production"],
            "registration_type": "manual",
        },
    ]


def test_server_card_data_attributes() -> None:
    env = _create_test_environment()
    template = env.get_template("servers.html")

    servers = _create_sample_servers()
    html = template.render(servers=servers)

    for server in servers:
        server_id = server["id"]

        assert "data-server-card" in html
        assert f'data-server-id="{server_id}"' in html
        assert f'data-server-name="{server["name"]}"' in html
        assert f'data-server-version="{server["version"]}"' in html
        assert f'data-server-description="{server["description"]}"' in html
        assert f'data-server-url="{server["url"]}"' in html
        assert f'data-server-tags="{",".join(server["tags"])}"' in html
        assert f'data-registration-type="{server["registration_type"]}"' in html


def test_health_status_htmx_binding() -> None:
    env = _create_test_environment()
    template = env.get_template("servers.html")

    servers = _create_sample_servers()
    html = template.render(servers=servers)

    for server in servers:
        server_id = server["id"]

        expected_hx_get = f'hx-get="/api/servers/{server_id}/health-status"'
        expected_hx_trigger = 'hx-trigger="load delay:500ms, every 20s"'
        expected_hx_swap = 'hx-swap="innerHTML"'

        assert expected_hx_get in html, f"Missing hx-get for {server_id}"
        assert expected_hx_trigger in html, f"Missing hx-trigger for {server_id}"
        assert expected_hx_swap in html, f"Missing hx-swap for {server_id}"


def test_tools_container_exists() -> None:
    env = _create_test_environment()
    template = env.get_template("servers.html")

    servers = _create_sample_servers()
    html = template.render(servers=servers)

    for server in servers:
        server_id = server["id"]

        assert f'id="tools-{server_id}"' in html
        assert 'class="hidden"' in html or "hidden" in html


def test_all_hidden_panel_containers() -> None:
    env = _create_test_environment()
    template = env.get_template("servers.html")

    servers = _create_sample_servers()
    html = template.render(servers=servers)

    for server in servers:
        server_id = server["id"]

        assert f'id="initialize-{server_id}"' in html
        assert f'id="trace-{server_id}"' in html
        assert f'id="faults-{server_id}"' in html
        assert f'id="playground-{server_id}"' in html


def test_template_renders_all_servers() -> None:
    env = _create_test_environment()
    template = env.get_template("servers.html")

    servers = _create_sample_servers()
    html = template.render(servers=servers)

    assert "Test Server One" in html
    assert "Test Server Two" in html
    assert "test-server-1" in html
    assert "test-server-2" in html


def test_template_renders_tags() -> None:
    env = _create_test_environment()
    template = env.get_template("servers.html")

    servers = _create_sample_servers()
    html = template.render(servers=servers)

    assert "testing" in html
    assert "demo" in html
    assert "production" in html


def test_empty_servers_shows_message() -> None:
    env = _create_test_environment()
    template = env.get_template("servers.html")

    html = template.render(servers=[])

    assert "No servers registered" in html


def test_vendored_scripts_included() -> None:
    env = _create_test_environment()
    template = env.get_template("servers.html")

    html = template.render(servers=[])

    assert "/static/vendor/htmx.min.js" in html
    assert "/static/vendor/hyperscript.min.js" in html
    assert "/static/vendor/tailwind.js" in html


def test_action_buttons_present() -> None:
    env = _create_test_environment()
    template = env.get_template("servers.html")

    servers = _create_sample_servers()
    html = template.render(servers=servers)

    assert "Initialize" in html
    assert "View Trace" in html
    assert "Faults" in html
    assert "Playground" in html
    assert "Refresh Capabilities" in html
