import pytest
from devhub.app import create_app


class TestConformanceScenarioTemplate:
    def test_passed_scenario_yields_official_passed_id_prefix(self) -> None:
        app = create_app()
        env = app.state.templates
        scenario = {
            "name": "test-scenario",
            "key": "scenario-key",
            "passed": 5,
            "failed": 0,
            "ok": True,
        }
        template = env.get_template("conformance_scenario.html")
        rendered = template.render(
            server_id="server-123",
            scenario=scenario,
            swap_oob="my-swap-target",
        )
        assert 'id="official-passed-server-123-scenario-key"' in rendered

    def test_passed_scenario_has_no_retest_button(self) -> None:
        app = create_app()
        env = app.state.templates
        scenario = {
            "name": "test-scenario",
            "key": "scenario-key",
            "passed": 5,
            "failed": 0,
            "ok": True,
        }
        template = env.get_template("conformance_scenario.html")
        rendered = template.render(
            server_id="server-123",
            scenario=scenario,
            swap_oob="my-swap-target",
        )
        assert "Retest" not in rendered

    def test_failed_scenario_yields_official_failed_id_prefix(self) -> None:
        app = create_app()
        env = app.state.templates
        scenario = {
            "name": "test-scenario",
            "key": "scenario-key",
            "passed": 3,
            "failed": 2,
            "ok": False,
        }
        template = env.get_template("conformance_scenario.html")
        rendered = template.render(
            server_id="server-123",
            scenario=scenario,
            swap_oob="my-swap-target",
        )
        assert 'id="official-failed-server-123-scenario-key"' in rendered

    def test_failed_scenario_has_retest_button(self) -> None:
        app = create_app()
        env = app.state.templates
        scenario = {
            "name": "test-scenario",
            "key": "scenario-key",
            "passed": 3,
            "failed": 2,
            "ok": False,
        }
        template = env.get_template("conformance_scenario.html")
        rendered = template.render(
            server_id="server-123",
            scenario=scenario,
            swap_oob="my-swap-target",
        )
        assert "Retest" in rendered
        assert (
            'action="/ui/server/server-123/conformance/official/retest?scenario=test-scenario"'
            in rendered
        )

    def test_failed_scenario_url_encodes_scenario_name(self) -> None:
        app = create_app()
        env = app.state.templates
        scenario = {
            "name": "test scenario with spaces",
            "key": "scenario-key",
            "passed": 3,
            "failed": 2,
            "ok": False,
        }
        template = env.get_template("conformance_scenario.html")
        rendered = template.render(
            server_id="server-123",
            scenario=scenario,
            swap_oob="my-swap-target",
        )
        assert "Retest" in rendered
        assert (
            "scenario=test+scenario+with+spaces" in rendered
            or "scenario=test%20scenario%20with%20spaces" in rendered
        )

    def test_wrapper_div_hx_swap_oob_equals_swap_oob_input(self) -> None:
        app = create_app()
        env = app.state.templates
        scenario = {
            "name": "test-scenario",
            "key": "scenario-key",
            "passed": 5,
            "failed": 0,
            "ok": True,
        }
        template = env.get_template("conformance_scenario.html")
        rendered = template.render(
            server_id="server-123",
            scenario=scenario,
            swap_oob="my-swap-target",
        )
        assert 'hx-swap-oob="my-swap-target"' in rendered

    def test_passed_scenario_shows_checkmark_and_counts(self) -> None:
        app = create_app()
        env = app.state.templates
        scenario = {
            "name": "my-scenario",
            "key": "scenario-key",
            "passed": 10,
            "failed": 0,
            "ok": True,
        }
        template = env.get_template("conformance_scenario.html")
        rendered = template.render(
            server_id="server-123",
            scenario=scenario,
            swap_oob="my-swap-target",
        )
        assert "my-scenario" in rendered
        assert "✓" in rendered
        assert "10/10" in rendered

    def test_failed_scenario_shows_x_mark_and_counts(self) -> None:
        app = create_app()
        env = app.state.templates
        scenario = {
            "name": "my-scenario",
            "key": "scenario-key",
            "passed": 7,
            "failed": 3,
            "ok": False,
        }
        template = env.get_template("conformance_scenario.html")
        rendered = template.render(
            server_id="server-123",
            scenario=scenario,
            swap_oob="my-swap-target",
        )
        assert "my-scenario" in rendered
        assert "✗" in rendered
        assert "7/10" in rendered
