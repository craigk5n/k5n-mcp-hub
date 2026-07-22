from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from mcp_hub.utils import dom_id


def _env() -> Environment:
    templates_dir = Path(__file__).parent.parent / "src" / "mcp_hub" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["dom_id"] = dom_id
    return env


def _render_one_tool() -> str:
    env = _env()
    tools = [
        {
            "name": "webcalendar/list-events",
            "description": "Retrieve calendar events.",
            "inputSchema": {
                "type": "object",
                "properties": {"start_date": {"type": "string", "description": "Start"}},
            },
        }
    ]
    return env.get_template("capabilities.html").render(
        server_id="webcalendar",
        server_name="WC",
        url="https://x/mcp",
        tools=tools,
        prompts=[],
        resources=[],
        debug_tools_json=None,
        cached=True,
        last_sync="",
        tool_count=1,
        prompt_count=0,
        resource_count=0,
        schema_conformant=False,
        schema_issues=[],
        error="",
    )


def test_tools_are_collapsible_single_column() -> None:
    html = _render_one_tool()
    # Each tool is a collapsible <details>/<summary> with a rotating chevron.
    assert "<details" in html
    assert "<summary" in html
    assert "group-open:rotate-90" in html


def test_invoke_button_has_busy_state() -> None:
    html = _render_one_tool()
    assert "hx-disabled-elt=\"find button[type='submit']\"" in html
    assert ">Invoke</span>" in html
    assert ">Invoking" in html


def test_invoke_target_uses_safe_dom_id() -> None:
    html = _render_one_tool()
    token = f"{dom_id('webcalendar')}-{dom_id('webcalendar/list-events')}"
    assert f'hx-target="#result-{token}"' in html
    assert f'id="result-{token}"' in html
