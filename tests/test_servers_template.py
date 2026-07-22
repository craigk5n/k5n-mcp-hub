import re

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path

from mcp_hub.utils import dom_id


def _create_test_environment() -> Environment:
    templates_dir = Path(__file__).parent.parent / "src" / "mcp_hub" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["dom_id"] = dom_id
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
        token = dom_id(server["id"])

        assert f'id="tools-{token}"' in html
        assert 'class="hidden"' in html or "hidden" in html


def test_all_hidden_panel_containers() -> None:
    env = _create_test_environment()
    template = env.get_template("servers.html")

    servers = _create_sample_servers()
    html = template.render(servers=servers)

    for server in servers:
        token = dom_id(server["id"])

        assert f'id="initialize-{token}"' in html
        assert f'id="trace-{token}"' in html
        assert f'id="faults-{token}"' in html
        assert f'id="playground-{token}"' in html


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


def test_panel_targets_safe_for_ids_with_spaces_and_dots() -> None:
    # Regression: a server id containing a space/dot (e.g. "k5n.us webcalendar")
    # must not leak into an hx-target selector like "#tools-k5n.us webcalendar",
    # which the browser parses as two selectors -> htmx:targetError.
    env = _create_test_environment()
    template = env.get_template("servers.html")

    server = {
        "id": "k5n.us webcalendar",
        "url": "http://localhost:8001/mcp",
        "name": "Weird Id Server",
        "version": "1.0.0",
        "description": "",
        "tags": [],
        "registration_type": "manual",
    }
    html = template.render(servers=[server])

    token = dom_id(server["id"])
    # Every panel target and its container use the sanitized token, which matches.
    for panel in ("tools", "initialize", "trace", "faults", "playground"):
        assert f'hx-target="#{panel}-{token}"' in html
        assert f'id="{panel}-{token}"' in html

    # The raw, unsafe selector must NOT appear anywhere.
    assert 'hx-target="#tools-k5n.us webcalendar"' not in html
    # No hx-target selector on the page contains a space (which would be invalid).
    for m in re.findall(r'hx-target="(#[^"]*)"', html):
        assert " " not in m, f"invalid selector with space: {m!r}"


def test_add_server_auth_type_dropdown() -> None:
    env = _create_test_environment()
    template = env.get_template("servers.html")

    html = template.render(servers=[])

    # Auth-type selector with None / Bearer / Basic options.
    assert 'name="auth_type"' in html
    assert '<option value="">None</option>' in html
    assert '<option value="bearer">' in html
    assert '<option value="basic">' in html

    # Conditional field groups for each scheme, hidden until selected.
    assert 'id="bearer-fields"' in html
    assert 'id="basic-fields"' in html
    assert 'name="bearer_token"' in html
    assert 'name="basic_username"' in html
    assert 'name="basic_password"' in html


def test_server_card_has_actions_menu_with_edit_and_delete() -> None:
    env = _create_test_environment()
    template = env.get_template("servers.html")

    server = {
        "id": "srv.one",
        "url": "http://localhost:8001/mcp",
        "name": "Server One",
        "version": "1.0.0",
        "description": "",
        "tags": [],
        "registration_type": "manual",
        "auth_type": "basic",
    }
    html = template.render(servers=[server])

    token = dom_id(server["id"])
    # Kebab menu with a safe, per-server id and Edit/Delete items wired to the dialogs.
    assert 'aria-label="Server actions"' in html
    assert f'id="menu-{token}"' in html
    assert "openEditServerDialog(closest <[data-server-card]/>)" in html
    assert "openDeleteServerDialog(closest <[data-server-card]/>)" in html
    # auth_type is exposed so the edit dialog can pre-select the scheme.
    assert 'data-server-auth-type="basic"' in html


def test_delete_confirmation_dialog_present() -> None:
    env = _create_test_environment()
    template = env.get_template("servers.html")
    html = template.render(servers=[])

    assert 'id="delete-server-dialog"' in html
    assert "confirmDeleteServer(this)" in html
    # Delete goes through the existing unregister endpoint.
    assert "'/v1/register/' + encodeURIComponent(id)" in html
    assert "method: 'DELETE'" in html


def test_add_server_button_shows_busy_state() -> None:
    # The submit button is disabled and shows a spinner/"Adding…" label while the
    # registration request (which triggers a backend discovery round-trip) is in flight.
    env = _create_test_environment()
    template = env.get_template("servers.html")

    html = template.render(servers=[])

    assert "hx-disabled-elt=\"find button[type='submit']\"" in html
    assert "animate-spin" in html
    assert ">Adding" in html


def test_action_buttons_present() -> None:
    env = _create_test_environment()
    template = env.get_template("servers.html")

    servers = _create_sample_servers()
    html = template.render(servers=servers)

    assert "Initialize" in html
    assert "View Trace" in html
    assert "Faults" in html
    assert "Playground" in html
    assert ">Capabilities</button>" in html
