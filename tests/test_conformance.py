import pytest
from fastapi.testclient import TestClient

from devhub.app import create_app


def test_conformance_template_renders_all_sections() -> None:
    app = create_app()
    templates = app.state.templates

    template = templates.get_template("conformance.html")

    context = {
        "server_id": "test-server-1",
        "protocol_version": "2024-11-05",
        "protocol_conformant": True,
        "schema_checked": True,
        "schema_conformant": True,
        "schema_issues": [],
        "tool_name_issues": [],
        "health_support": True,
        "oauth_status": "ok",
        "tool_count": 5,
        "prompt_count": 3,
        "resource_count": 10,
    }

    html = template.render(**context)

    assert "Protocol Version" in html
    assert "MCP 2024-11-05" in html
    assert "Schema Validation" in html
    assert "Valid" in html
    assert "Tool Names" in html
    assert "Health Support" in html
    assert "OAuth Status" in html
    assert "Resource Counts" in html
    assert "Tools" in html
    assert "Prompts" in html
    assert "Resources" in html
    assert "5" in html
    assert "3" in html
    assert "10" in html


def test_conformance_template_renders_schema_issues() -> None:
    app = create_app()
    templates = app.state.templates

    template = templates.get_template("conformance.html")

    context = {
        "server_id": "test-server-1",
        "protocol_version": "2024-11-05",
        "protocol_conformant": True,
        "schema_checked": True,
        "schema_conformant": False,
        "schema_issues": ["Missing required field 'name'", "Invalid type for 'version'"],
        "tool_name_issues": [],
        "health_support": True,
        "oauth_status": "ok",
        "tool_count": 5,
        "prompt_count": 3,
        "resource_count": 10,
    }

    html = template.render(**context)

    assert "Schema issues:" in html
    assert "Missing required field &#39;name&#39;" in html
    assert "Invalid type for &#39;version&#39;" in html
    assert "Issues (2)" in html


def test_conformance_template_renders_tool_name_issues() -> None:
    app = create_app()
    templates = app.state.templates

    template = templates.get_template("conformance.html")

    context = {
        "server_id": "test-server-1",
        "protocol_version": "2024-11-05",
        "protocol_conformant": True,
        "schema_checked": True,
        "schema_conformant": True,
        "schema_issues": [],
        "tool_name_issues": [
            "Tool 'invalid-name' contains spaces",
            "Tool '__private' starts with underscore",
        ],
        "health_support": True,
        "oauth_status": "ok",
        "tool_count": 5,
        "prompt_count": 3,
        "resource_count": 10,
    }

    html = template.render(**context)

    assert "Tool name issues:" in html
    assert "Tool &#39;invalid-name&#39; contains spaces" in html
    assert "Tool &#39;__private&#39; starts with underscore" in html
    assert "Issues (2)" in html


def test_conformance_template_official_conformance_button() -> None:
    app = create_app()
    templates = app.state.templates

    template = templates.get_template("conformance.html")

    context = {
        "server_id": "my-test-server",
        "protocol_version": "2024-11-05",
        "protocol_conformant": True,
        "schema_checked": True,
        "schema_conformant": True,
        "schema_issues": [],
        "tool_name_issues": [],
        "health_support": True,
        "oauth_status": "ok",
        "tool_count": 5,
        "prompt_count": 3,
        "resource_count": 10,
    }

    html = template.render(**context)

    assert 'hx-get="/ui/server/my-test-server/conformance/official/status"' in html
    assert "Switch to Official Conformance" in html


def test_conformance_template_protocol_not_conformant() -> None:
    app = create_app()
    templates = app.state.templates

    template = templates.get_template("conformance.html")

    context = {
        "server_id": "test-server-1",
        "protocol_version": "2024-11-05",
        "protocol_conformant": False,
        "schema_checked": True,
        "schema_conformant": True,
        "schema_issues": [],
        "tool_name_issues": [],
        "health_support": True,
        "oauth_status": "ok",
        "tool_count": 5,
        "prompt_count": 3,
        "resource_count": 10,
    }

    html = template.render(**context)

    assert "⚠ MCP 2024-11-05" in html


def test_conformance_template_oauth_status_variants() -> None:
    app = create_app()
    templates = app.state.templates

    template = templates.get_template("conformance.html")

    context = {
        "server_id": "test-server-1",
        "protocol_version": "2024-11-05",
        "protocol_conformant": True,
        "schema_checked": True,
        "schema_conformant": True,
        "schema_issues": [],
        "tool_name_issues": [],
        "health_support": True,
        "oauth_status": None,
        "tool_count": 5,
        "prompt_count": 3,
        "resource_count": 10,
    }

    html = template.render(**context)
    assert "Not configured" in html

    context["oauth_status"] = "error"
    html = template.render(**context)
    assert "Error" in html

    context["oauth_status"] = "pending"
    html = template.render(**context)
    assert "pending" in html


def test_conformance_template_health_support_variants() -> None:
    app = create_app()
    templates = app.state.templates

    template = templates.get_template("conformance.html")

    context = {
        "server_id": "test-server-1",
        "protocol_version": "2024-11-05",
        "protocol_conformant": True,
        "schema_checked": True,
        "schema_conformant": True,
        "schema_issues": [],
        "tool_name_issues": [],
        "health_support": "no /health endpoint",
        "oauth_status": "ok",
        "tool_count": 5,
        "prompt_count": 3,
        "resource_count": 10,
    }

    html = template.render(**context)
    assert "Not Supported" in html

    context["health_support"] = "supports /health"
    html = template.render(**context)
    assert "Supported" in html


def test_conformance_template_schema_not_checked() -> None:
    app = create_app()
    templates = app.state.templates

    template = templates.get_template("conformance.html")

    context = {
        "server_id": "test-server-1",
        "protocol_version": "2024-11-05",
        "protocol_conformant": True,
        "schema_checked": False,
        "schema_conformant": False,
        "schema_issues": [],
        "tool_name_issues": [],
        "health_support": True,
        "oauth_status": "ok",
        "tool_count": 5,
        "prompt_count": 3,
        "resource_count": 10,
    }

    html = template.render(**context)
    assert "Not checked" in html
