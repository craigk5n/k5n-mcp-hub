"""Admin-UI surfacing for on-behalf-of servers (Story 7.1).

Also closes the two acceptance criteria deferred from Story 6.5: an OBO-only server's
degraded background state, and the provenance of cached capabilities.
"""

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
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    environment.filters["dom_id"] = dom_id
    return environment


def obo_server(**overrides) -> RegisteredServer:
    fields = {
        "id": "files",
        "url": "https://files.example.com/mcp",
        "name": "Files",
        "auth_type": "obo",
        "oauth_token_url": "https://idp.example.com/token",
        "oauth_client_id": "k5n-mcp-hub",
        "oauth_client_secret": "hub-secret",
        "obo_audience": "mcp-server-files",
    }
    fields.update(overrides)
    return RegisteredServer(**fields)


def render_servers(env: Environment, servers: list) -> str:
    return env.get_template("servers.html").render(servers=servers)


class TestRegistrationForm:
    def test_obo_is_an_option(self, env: Environment) -> None:
        html = render_servers(env, [])

        assert 'value="obo"' in html

    def test_obo_field_group_exists_and_starts_hidden(self, env: Environment) -> None:
        html = render_servers(env, [])

        assert 'id="obo-fields"' in html
        group = html[html.index('id="obo-fields"') - 200 : html.index('id="obo-fields"') + 40]
        assert "hidden" in group

    @pytest.mark.parametrize(
        "field",
        ["obo_audience", "obo_resource", "obo_scope", "obo_actor_token_source"],
    )
    def test_obo_inputs_are_present(self, env: Environment, field: str) -> None:
        assert f'name="{field}"' in render_servers(env, [])

    @pytest.mark.parametrize("field", ["oauth_token_url", "oauth_client_id", "oauth_client_secret"])
    def test_the_hubs_own_client_credentials_are_collectable(
        self, env: Environment, field: str
    ) -> None:
        # OBO authenticates the exchange with the hub's own client, so the form has to
        # collect it -- there was no OAuth field group in the UI before this.
        assert f'name="{field}"' in render_servers(env, [])

    def test_client_secret_is_a_password_input(self, env: Environment) -> None:
        html = render_servers(env, [])
        idx = html.index('name="oauth_client_secret"')

        assert 'type="password"' in html[idx - 120 : idx]

    def test_visibility_toggle_covers_obo(self, env: Environment) -> None:
        html = render_servers(env, [])

        assert "obo-fields" in html
        assert "authType !== 'obo'" in html


class TestServerCard:
    def test_obo_badge_names_the_audience(self, env: Environment) -> None:
        html = render_servers(env, [obo_server()])

        assert "data-obo-badge" in html
        assert "mcp-server-files" in html

    def test_delegation_is_shown_when_active(self, env: Environment) -> None:
        html = render_servers(env, [obo_server(obo_actor_token_source="client_credentials")])

        assert "data-obo-delegation" in html

    def test_delegation_not_shown_for_impersonation(self, env: Environment) -> None:
        # Targets the card marker, not the words: the registration form always lists
        # "Delegation" as a selectable option.
        html = render_servers(env, [obo_server()])

        assert "data-obo-delegation" not in html

    def test_exchange_error_is_surfaced(self, env: Environment) -> None:
        html = render_servers(
            env, [obo_server(obo_status="error", obo_error="invalid_target: audience not found")]
        )

        assert "invalid_target" in html

    def test_a_healthy_exchange_is_shown(self, env: Environment) -> None:
        html = render_servers(env, [obo_server(obo_status="ok")])

        assert "data-obo-badge" in html
        assert "bg-green-100" in html

    def test_non_obo_server_shows_no_obo_markup(self, env: Environment) -> None:
        plain = RegisteredServer(id="s", url="http://x", name="Plain")

        html = render_servers(env, [plain])

        assert "data-obo-badge" not in html


class TestDegradedBackgroundState:
    """The Story 6.5 criteria that were deferred here."""

    def test_obo_only_server_states_that_it_needs_a_user_session(self, env: Environment) -> None:
        bare = obo_server(oauth_client_id="", oauth_client_secret="")

        html = render_servers(env, [bare])

        assert "user session" in html

    def test_server_with_a_service_credential_shows_no_such_note(self, env: Environment) -> None:
        html = render_servers(env, [obo_server(bearer_token="service-token")])

        assert "user session" not in html


class TestCapabilitiesProvenance:
    def test_service_identity_label_when_capabilities_are_shared(self, env: Environment) -> None:
        # A backend that varies its tool list per user would otherwise show one
        # identity's list to everyone with no indication.
        html = env.get_template("capabilities.html").render(
            server=obo_server(bearer_token="service-token"),
            tools=[{"name": "search"}],
            prompts=[],
            resources=[],
            cached=True,
            error_message="",
            last_sync=None,
        )

        assert "service identity" in html.lower()

    def test_no_label_for_a_non_obo_server(self, env: Environment) -> None:
        html = env.get_template("capabilities.html").render(
            server=RegisteredServer(id="s", url="http://x", name="Plain"),
            tools=[{"name": "search"}],
            prompts=[],
            resources=[],
            cached=True,
            error_message="",
            last_sync=None,
        )

        assert "service identity" not in html.lower()


class TestProvenanceReachesTheRealRoute:
    """A template-only test passes `server` in by hand; the route has to actually
    supply it, and originally did not."""

    def test_capabilities_page_shows_provenance_for_an_obo_server(self) -> None:
        import anyio
        from fastapi.testclient import TestClient

        from mcp_hub.app import create_app
        from mcp_hub.config import Settings

        app = create_app(Settings.from_defaults())
        anyio.run(app.state.registry.register, obo_server(bearer_token="service-token"))

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/ui/server/files/capabilities")

        assert response.status_code == 200
        assert "data-capabilities-provenance" in response.text

    def test_capabilities_page_has_no_banner_for_a_plain_server(self) -> None:
        import anyio
        from fastapi.testclient import TestClient

        from mcp_hub.app import create_app
        from mcp_hub.config import Settings

        app = create_app(Settings.from_defaults())
        anyio.run(
            app.state.registry.register,
            RegisteredServer(id="plain", url="http://x.example", name="Plain"),
        )

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/ui/server/plain/capabilities")

        assert response.status_code == 200
        assert "data-capabilities-provenance" not in response.text
