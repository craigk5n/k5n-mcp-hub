import pytest
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path


def _create_test_environment() -> Environment:
    templates_dir = Path(__file__).parent.parent / "src" / "mcp_hub" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return env


def _create_sample_agents() -> list[dict]:
    return [
        {
            "id": "test-agent-1",
            "url": "http://localhost:8001/agent",
            "name": "Test Agent One",
            "version": "1.0.0",
            "description": "A test agent for unit testing",
            "card_valid": True,
            "card_issues": [],
            "last_card_checked": None,
        },
        {
            "id": "test-agent-2",
            "url": "http://localhost:8002/agent",
            "name": "Test Agent Two",
            "version": "2.1.3",
            "description": "Another test agent",
            "card_valid": False,
            "card_issues": [
                "drift: version expected='2.0.0' actual='2.1.3'",
                "missing capability: tools",
            ],
            "last_card_checked": None,
        },
    ]


class TestAgentCardStatusTemplate:
    def test_valid_agent_shows_valid_badge(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_card_status.html")

        agent: dict[str, Any] = {
            "id": "test-agent",
            "name": "Test Agent",
            "card_valid": True,
            "card_issues": [],
        }
        html = template.render(agent=agent)

        assert "✓ Valid" in html

    def test_invalid_agent_shows_invalid_badge(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_card_status.html")

        agent: dict[str, Any] = {
            "id": "test-agent",
            "name": "Test Agent",
            "card_valid": False,
            "card_issues": ["some issue"],
        }
        html = template.render(agent=agent)

        assert "✗ Invalid" in html

    def test_unknown_validity_shows_unknown_badge(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_card_status.html")

        agent: dict[str, Any] = {
            "id": "test-agent",
            "name": "Test Agent",
            "card_valid": None,
            "card_issues": [],
        }
        html = template.render(agent=agent)

        assert "🕐 Unknown" in html

    def test_card_issues_displayed(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_card_status.html")

        agent: dict[str, Any] = {
            "id": "test-agent",
            "name": "Test Agent",
            "card_valid": False,
            "card_issues": ["drift: version expected='1.0' actual='2.0'", "missing skill: foo"],
        }
        html = template.render(agent=agent)

        assert "drift: version expected=&#39;1.0&#39; actual=&#39;2.0&#39;" in html
        assert "missing skill: foo" in html


class TestAgentsTemplate:
    def test_template_renders_without_jinja_errors(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agents.html")

        agents = _create_sample_agents()
        html = template.render(agents=agents)

        assert html is not None
        assert len(html) > 0

    def test_agent_card_data_attributes(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agents.html")

        agents = _create_sample_agents()
        html = template.render(agents=agents)

        for agent in agents:
            agent_id = agent["id"]

            assert "data-agent-card" in html
            assert f'data-agent-id="{agent_id}"' in html
            assert f'data-agent-name="{agent["name"]}"' in html
            assert f'data-agent-version="{agent["version"]}"' in html
            assert f'data-agent-description="{agent["description"]}"' in html
            assert f'data-agent-url="{agent["url"]}"' in html

    def test_card_status_htmx_binding(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agents.html")

        agents = _create_sample_agents()
        html = template.render(agents=agents)

        for agent in agents:
            agent_id = agent["id"]

            expected_hx_get = f'hx-get="/api/agents/{agent_id}/card-status"'
            expected_hx_trigger = 'hx-trigger="load delay:500ms, every 20s"'
            expected_hx_swap = 'hx-swap="innerHTML"'

            assert expected_hx_get in html, f"Missing hx-get for {agent_id}"
            assert expected_hx_trigger in html, f"Missing hx-trigger for {agent_id}"
            assert expected_hx_swap in html, f"Missing hx-swap for {agent_id}"

    def test_workbench_container_exists(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agents.html")

        agents = _create_sample_agents()
        html = template.render(agents=agents)

        for agent in agents:
            agent_id = agent["id"]

            assert f'id="workbench-{agent_id}"' in html

    def test_card_status_container_exists(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agents.html")

        agents = _create_sample_agents()
        html = template.render(agents=agents)

        for agent in agents:
            agent_id = agent["id"]

            assert f'id="card-status-{agent_id}"' in html

    def test_action_buttons_present(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agents.html")

        agents = _create_sample_agents()
        html = template.render(agents=agents)

        assert "Open Workbench" in html
        assert "Refresh Card" in html

    def test_empty_agents_shows_message(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agents.html")

        html = template.render(agents=[])

        assert "No agents registered" in html

    def test_vendored_scripts_included(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agents.html")

        html = template.render(agents=[])

        assert "/static/vendor/htmx.min.js" in html
        assert "/static/vendor/hyperscript.min.js" in html
        assert "/static/vendor/tailwind.js" in html


class TestAgentWorkbenchTemplate:
    def test_template_renders_without_jinja_errors(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_workbench.html")

        agent = {
            "id": "test-agent",
            "name": "Test Agent",
            "url": "http://localhost:8001/agent",
        }
        fixtures = [
            {"id": "fixture-1", "name": "List Tools"},
            {"id": "fixture-2", "name": "Echo"},
        ]
        stock_payload = '{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}'

        html = template.render(agent=agent, fixtures=fixtures, stock_payload=stock_payload)

        assert html is not None
        assert len(html) > 0

    def test_agent_name_displayed(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_workbench.html")

        agent = {"id": "test-agent", "name": "My Test Agent", "url": "http://localhost:8001/agent"}

        html = template.render(agent=agent, fixtures=[], stock_payload="")

        assert "My Test Agent" in html
        assert "Agent Workbench" in html

    def test_saved_fixtures_dropdown(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_workbench.html")

        agent = {"id": "test-agent", "name": "Test Agent", "url": "http://localhost:8001/agent"}
        fixtures = [
            {"id": "fixture-1", "name": "List Tools"},
            {"id": "fixture-2", "name": "Call Tool Foo"},
        ]

        html = template.render(agent=agent, fixtures=fixtures, stock_payload="")

        assert "Saved Fixtures" in html
        assert "List Tools" in html
        assert "Call Tool Foo" in html

    def test_invoke_button_present(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_workbench.html")

        agent = {"id": "test-agent", "name": "Test Agent", "url": "http://localhost:8001/agent"}

        html = template.render(agent=agent, fixtures=[], stock_payload="")

        assert 'hx-post="/ui/agent/test-agent/invoke"' in html
        assert "Invoke" in html

    def test_save_as_fixture_button_present(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_workbench.html")

        agent = {"id": "test-agent", "name": "Test Agent", "url": "http://localhost:8001/agent"}

        html = template.render(agent=agent, fixtures=[], stock_payload="")

        assert 'hx-post="/ui/agent/test-agent/save-as-fixture"' in html
        assert "Save as Fixture" in html

    def test_stock_payload_textarea(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_workbench.html")

        agent = {"id": "test-agent", "name": "Test Agent", "url": "http://localhost:8001/agent"}
        stock_payload = '{"jsonrpc": "2.0", "id": 1}'

        html = template.render(agent=agent, fixtures=[], stock_payload=stock_payload)

        assert 'id="stock-payload"' in html
        assert "&#34;jsonrpc&#34;: &#34;2.0&#34;, &#34;id&#34;: 1" in html

    def test_request_id_input(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_workbench.html")

        agent = {"id": "test-agent", "name": "Test Agent", "url": "http://localhost:8001/agent"}

        html = template.render(agent=agent, fixtures=[], stock_payload="")

        assert 'id="request-id"' in html
        assert "Request ID" in html

    def test_headers_textarea(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_workbench.html")

        agent = {"id": "test-agent", "name": "Test Agent", "url": "http://localhost:8001/agent"}

        html = template.render(agent=agent, fixtures=[], stock_payload="")

        assert 'id="headers"' in html
        assert "Headers" in html

    def test_invoke_result_container(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_workbench.html")

        agent = {"id": "test-agent", "name": "Test Agent", "url": "http://localhost:8001/agent"}

        html = template.render(agent=agent, fixtures=[], stock_payload="")

        assert 'id="invoke-result-test-agent"' in html


class TestAgentInvokeResultTemplate:
    def test_template_renders_without_jinja_errors(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_invoke_result.html")

        result = {
            "status": 200,
            "duration_ms": 150,
            "headers": '{"Content-Type": "application/json"}',
            "request_body": '{"jsonrpc": "2.0", "id": 1}',
            "response_body": '{"jsonrpc": "2.0", "id": 1, "result": {}}',
            "error": None,
        }

        html = template.render(**result)

        assert html is not None
        assert len(html) > 0

    def test_success_status_shows_green_badge(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_invoke_result.html")

        result = {
            "status": 200,
            "duration_ms": 100,
            "headers": "{}",
            "request_body": "{}",
            "response_body": "{}",
            "error": None,
        }

        html = template.render(**result)

        assert "✓ 200" in html
        assert "Duration: 100ms" in html

    def test_error_status_shows_red_badge(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_invoke_result.html")

        result = {
            "status": 500,
            "duration_ms": 50,
            "headers": "{}",
            "request_body": "{}",
            "response_body": "Internal Server Error",
            "error": None,
        }

        html = template.render(**result)

        assert "✗ 500" in html

    def test_error_displayed(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_invoke_result.html")

        result = {
            "status": 500,
            "duration_ms": 50,
            "headers": "{}",
            "request_body": "{}",
            "response_body": "",
            "error": "Connection refused: localhost:8001",
        }

        html = template.render(**result)

        assert "Error" in html
        assert "Connection refused: localhost:8001" in html
        assert "bg-red-50" in html

    def test_request_body_displayed(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_invoke_result.html")

        result = {
            "status": 200,
            "duration_ms": 100,
            "headers": "{}",
            "request_body": '{"jsonrpc": "2.0", "method": "tools/list"}',
            "response_body": '{"jsonrpc": "2.0", "id": 1}',
            "error": None,
        }

        html = template.render(**result)

        assert "Request" in html
        assert "&#34;jsonrpc&#34;: &#34;2.0&#34;, &#34;method&#34;: &#34;tools/list&#34;" in html

    def test_response_body_displayed(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_invoke_result.html")

        result = {
            "status": 200,
            "duration_ms": 100,
            "headers": "{}",
            "request_body": "{}",
            "response_body": '{"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}',
            "error": None,
        }

        html = template.render(**result)

        assert "Response" in html
        assert (
            "&#34;jsonrpc&#34;: &#34;2.0&#34;, &#34;id&#34;: 1, &#34;result&#34;: {&#34;tools&#34;: []}"
            in html
        )

    def test_headers_displayed(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_invoke_result.html")

        result = {
            "status": 200,
            "duration_ms": 100,
            "headers": '{"Content-Type": "application/json", "X-Request-Id": "abc123"}',
            "request_body": "{}",
            "response_body": "{}",
            "error": None,
        }

        html = template.render(**result)

        assert "Headers" in html
        assert "Content-Type" in html

    def test_no_headers_hides_section(self) -> None:
        env = _create_test_environment()
        template = env.get_template("agent_invoke_result.html")

        result = {
            "status": 200,
            "duration_ms": 100,
            "headers": "",
            "request_body": "{}",
            "response_body": "{}",
            "error": None,
        }

        html = template.render(**result)

        assert "Headers" not in html
