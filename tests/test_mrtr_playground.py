"""The playground surfaces an input_required result (Story 4.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from mcp_hub.mcp.mrtr import build_retry_body, parse_input_required
from mcp_hub.utils import dom_id, pretty_json

TEMPLATES = Path(__file__).parent.parent / "src" / "mcp_hub" / "templates"

INPUT_REQUIRED_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "resultType": "input_required",
            "inputRequests": {
                "github_login": {
                    "method": "elicitation/create",
                    "params": {
                        "mode": "form",
                        "message": "Please provide your GitHub username",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                        },
                    },
                }
            },
            "requestState": "opaque-state",
        },
    }
)

ORIGINAL = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "get_weather", "arguments": {"location": "New York"}},
    }
)


@pytest.fixture
def env() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html", "xml"])
    )
    environment.filters["dom_id"] = dom_id
    environment.filters["pretty_json"] = pretty_json
    return environment


def render(env: Environment, **overrides) -> str:
    context = {
        "server_id": "s",
        "url": "http://x/mcp",
        "request_body": ORIGINAL,
        "session_id": "",
        "protocol_version": "2026-07-28",
        "accept_sse": False,
        "error": "",
        "parse_error": "",
        "stateless": True,
        "input_required": None,
        "retry_body": "",
        # The whole request/response grid is gated on a status; a real
        # input_required response always carries one.
        "response_status": 200,
        "response_headers": "",
        "response_body": INPUT_REQUIRED_BODY,
        "request_headers": "",
        "parsed_body": "",
        "auth_hint": "",
    }
    context.update(overrides)
    return env.get_template("playground.html").render(**context)


class TestRendering:
    def test_input_requests_are_shown(self, env: Environment) -> None:
        parsed = parse_input_required(INPUT_REQUIRED_BODY)

        html = render(env, input_required=parsed)

        assert "data-input-required" in html
        assert "Please provide your GitHub username" in html
        assert "github_login" in html

    def test_a_prefilled_retry_is_offered(self, env: Environment) -> None:
        parsed = parse_input_required(INPUT_REQUIRED_BODY)
        assert parsed is not None
        retry = build_retry_body(ORIGINAL, parsed)
        assert retry is not None

        html = render(env, input_required=parsed, retry_body=json.dumps(retry, indent=2))

        assert "inputResponses" in html
        assert "data-load-retry" in html

    def test_nothing_is_shown_for_an_ordinary_response(self, env: Environment) -> None:
        html = render(env)

        assert "data-input-required" not in html


class TestRoute:
    def test_the_route_passes_the_parsed_result_to_the_template(self) -> None:
        # A template-only test supplies the context by hand; this pins that the route
        # actually builds it -- the same gap that hid the capabilities banner.
        import inspect

        from mcp_hub.routes import ui_playground

        source = inspect.getsource(ui_playground)
        assert "input_required=" in source
        assert "retry_body=" in source
        assert "parse_input_required" in source
