"""Admin-UI surfacing for Enterprise-Managed Authorization servers (Story 8.6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from mcp_hub.models.server import RegisteredServer
from mcp_hub.utils import dom_id

TEMPLATES = Path(__file__).parent.parent / "src" / "mcp_hub" / "templates"


@pytest.fixture
def env() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html", "xml"])
    )
    environment.filters["dom_id"] = dom_id
    return environment


def ema_server(**overrides) -> RegisteredServer:
    fields = {
        "id": "files",
        "url": "https://files.example.com/mcp",
        "name": "Files",
        "auth_type": "ema",
        "oauth_token_url": "https://idp.example.com/token",
        "oauth_client_id": "k5n-mcp-hub",
        "oauth_client_secret": "hub-secret",
        "ema_resource_as_issuer": "https://backend.example.com/oauth",
        "ema_resource_as_token_url": "https://backend.example.com/oauth/token",
        "ema_resource_id": "https://files.example.com/mcp",
    }
    fields.update(overrides)
    return RegisteredServer(**fields)


def render(env: Environment, servers: list) -> str:
    return env.get_template("servers.html").render(servers=servers)


class TestRegistrationForm:
    def test_ema_is_an_option(self, env: Environment) -> None:
        assert 'value="ema"' in render(env, [])

    def test_field_group_exists_and_starts_hidden(self, env: Environment) -> None:
        html = render(env, [])
        idx = html.index('id="ema-fields"')

        assert "hidden" in html[idx - 200 : idx + 40]

    @pytest.mark.parametrize(
        "field",
        [
            "ema_resource_as_issuer",
            "ema_resource_as_token_url",
            "ema_resource_id",
            "ema_subject_token_type",
        ],
    )
    def test_inputs_are_present(self, env: Environment, field: str) -> None:
        assert f'name="{field}"' in render(env, [])

    def test_visibility_toggle_covers_ema(self, env: Environment) -> None:
        assert "authType !== 'ema'" in render(env, [])


class TestServerCard:
    def test_badge_names_the_resource_authorization_server(self, env: Environment) -> None:
        html = render(env, [ema_server()])

        assert "data-ema-badge" in html
        assert "backend.example.com/oauth" in html

    def test_which_leg_failed_is_shown(self, env: Environment) -> None:
        # "the IdP refused" and "the backend's AS refused" need different fixes.
        html = render(
            env, [ema_server(ema_status="error", ema_error="leg 2 (resource AS): invalid_grant")]
        )

        assert "leg 2" in html

    def test_a_healthy_exchange_is_shown(self, env: Environment) -> None:
        html = render(env, [ema_server(ema_status="ok")])

        assert "data-ema-badge" in html
        assert "bg-green-100" in html

    def test_id_token_mode_is_visible(self, env: Environment) -> None:
        # Which subject token the hub sends is the setting most likely to be wrong.
        assert "ID token" in render(env, [ema_server()])

    def test_access_token_mode_is_visible(self, env: Environment) -> None:
        html = render(env, [ema_server(ema_subject_token_type="access_token")])

        assert "access token" in html

    def test_non_ema_server_shows_no_ema_markup(self, env: Environment) -> None:
        assert "data-ema-badge" not in render(
            env, [RegisteredServer(id="s", url="http://x", name="Plain")]
        )

    def test_ema_only_server_states_that_it_needs_a_user_session(self, env: Environment) -> None:
        bare = ema_server(oauth_client_id="", oauth_client_secret="")

        assert "user session" in render(env, [bare])
