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


def _render_tool(properties: dict, required: list[str] | None = None) -> str:
    tools = [
        {
            "name": "findPetsByStatus",
            "inputSchema": {
                "type": "object",
                "properties": properties,
                **({"required": required} if required else {}),
            },
        }
    ]
    return (
        _env()
        .get_template("capabilities.html")
        .render(
            server_id="pets",
            server_name="Pets",
            url="openapi:pets",
            tools=tools,
            prompts=[],
            resources=[],
            debug_tools_json=None,
            cached=True,
            last_sync="",
            tool_count=1,
            prompt_count=0,
            resource_count=0,
            schema_conformant=True,
            schema_issues=[],
            error="",
        )
    )


def _select(html: str) -> str:
    start = html.index('<select id="status"')
    return html[start : html.index("</select>", start)]


STATUS = {"type": "string", "enum": ["available", "pending", "sold"]}


def test_an_enum_is_a_dropdown_of_its_values() -> None:
    select = _select(_render_tool({"status": STATUS}))
    assert 'name="status"' in select
    for value in ("available", "pending", "sold"):
        assert f'<option value="{value}"' in select


def test_an_optional_enum_can_be_left_unset() -> None:
    select = _select(_render_tool({"status": STATUS}))
    # An empty value is skipped by the form handler, so the argument is omitted.
    assert '<option value=""' in select


def test_a_required_enum_has_no_empty_choice() -> None:
    select = _select(_render_tool({"status": STATUS}, required=["status"]))
    assert '<option value=""' not in select


def test_the_default_is_preselected() -> None:
    select = _select(_render_tool({"status": {**STATUS, "default": "available"}}))
    assert '<option value="available" selected' in select
    assert '<option value="sold" selected' not in select


def test_non_string_values_submit_as_text() -> None:
    select = _select(_render_tool({"status": {"type": "integer", "enum": [1, 2]}}))
    assert '<option value="1"' in select and '<option value="2"' in select


def test_values_are_escaped() -> None:
    html = _render_tool({"status": {"type": "string", "enum": ['"><script>x</script>']}})
    assert "<script>x</script>" not in html


def test_a_field_without_an_enum_is_still_a_text_box() -> None:
    html = _render_tool({"status": {"type": "string"}})
    assert '<select id="status"' not in html
    assert 'id="status"' in html
