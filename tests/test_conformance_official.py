import pytest

from devhub.app import create_app


class TestConformanceOfficialTemplate:
    @pytest.fixture
    def app(self):
        return create_app()

    @pytest.fixture
    def template(self, app):
        return app.state.templates.get_template("conformance_official.html")

    @pytest.mark.parametrize(
        "available,missing,expected_run_button,expected_missing_section,dep_name",
        [
            (
                False,
                [{"name": "mcp-runner", "hint": "pip install mcp-runner"}],
                False,
                True,
                "mcp-runner",
            ),
            (
                False,
                [
                    {"name": "uv", "hint": "pip install uv"},
                    {"name": "node", "hint": "Install Node.js"},
                ],
                False,
                True,
                "uv",
            ),
        ],
    )
    def test_not_available_renders_missing_deps_and_no_run_button(
        self,
        template,
        available,
        missing,
        expected_run_button,
        expected_missing_section,
        dep_name,
    ):
        context = {
            "server_id": "test-server",
            "target_url": "http://localhost:3000",
            "available": available,
            "missing": missing,
            "command": "",
            "output": "",
            "exit_code": 0,
            "has_run": False,
            "scenarios": [],
            "total_pass": 0,
            "total_fail": 0,
        }

        html = template.render(**context)

        if expected_missing_section:
            assert "Missing Dependencies" in html
            assert dep_name in html
        if not expected_run_button:
            assert 'hx-post="/ui/server/test-server/conformance/official/run"' not in html

    def test_available_renders_run_button(self, template):
        context = {
            "server_id": "my-server-123",
            "target_url": "http://localhost:8080",
            "available": True,
            "missing": [],
            "command": "",
            "output": "",
            "exit_code": 0,
            "has_run": False,
            "scenarios": [],
            "total_pass": 0,
            "total_fail": 0,
        }

        html = template.render(**context)

        assert 'hx-post="/ui/server/my-server-123/conformance/official/run"' in html

    def test_has_run_renders_results(self, template):
        context = {
            "server_id": "test-server",
            "target_url": "http://localhost:3000",
            "available": True,
            "missing": [],
            "command": "mcp-runner run --target http://localhost:3000",
            "output": "Running conformance tests...\nTest 1: PASS\nTest 2: FAIL",
            "exit_code": 1,
            "has_run": True,
            "scenarios": [
                {"name": "initialize", "passed": True, "error": ""},
                {"name": "tools/list", "passed": False, "error": "timeout after 5s"},
                {"name": "resources/list", "passed": True, "error": ""},
            ],
            "total_pass": 2,
            "total_fail": 1,
        }

        html = template.render(**context)

        assert "Run Results" in html
        assert "mcp-runner run --target http://localhost:3000" in html
        assert "1" in html
        assert "2" in html
        assert "Passed" in html
        assert "Failed" in html
        assert "tools/list" in html
        assert "initialize" in html
        assert "resources/list" in html
        assert "Retest" in html
        assert "<details" in html
        assert "Raw Output" in html


class TestConformanceOfficialTemplateVariables:
    def test_snake_case_variables(self):
        app = create_app()
        template = app.state.templates.get_template("conformance_official.html")

        context = {
            "server_id": "test",
            "target_url": "http://test",
            "available": True,
            "missing": [],
            "command": "test cmd",
            "output": "test output",
            "exit_code": 0,
            "has_run": True,
            "scenarios": [],
            "total_pass": 0,
            "total_fail": 0,
        }

        html = template.render(**context)
        assert "server_id" not in html
        assert "total_pass" not in html
        assert "total_fail" not in html


class TestConformanceOfficialTemplateHxPost:
    @pytest.mark.parametrize(
        "server_id,expected",
        [
            ("server-1", "/ui/server/server-1/conformance/official/run"),
            ("abc123", "/ui/server/abc123/conformance/official/run"),
            ("my-server", "/ui/server/my-server/conformance/official/run"),
        ],
    )
    def test_run_button_has_correct_hx_post_url(self, server_id, expected):
        app = create_app()
        template = app.state.templates.get_template("conformance_official.html")

        context = {
            "server_id": server_id,
            "target_url": "http://localhost:3000",
            "available": True,
            "missing": [],
            "command": "",
            "output": "",
            "exit_code": 0,
            "has_run": False,
            "scenarios": [],
            "total_pass": 0,
            "total_fail": 0,
        }

        html = template.render(**context)
        assert f'hx-post="{expected}"' in html
