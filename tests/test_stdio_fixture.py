"""The in-tree stdio server the rest of Epic 9 is tested against.

Deliberately driven as a real subprocess over real pipes rather than mocked. Every
significant bug found in this codebase's MCP client work has been one where a test
double agreed with the implementation and the SDK did not -- the lenient-parse
fallback passed four mocked tests while being useless in production. A fixture that
is not exercised end to end is not a fixture, it is a second opinion from the same
source.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ECHO_SERVER = Path(__file__).parent / "fixtures" / "echo_stdio_server.py"


def test_fixture_file_exists() -> None:
    assert ECHO_SERVER.is_file(), f"missing stdio fixture at {ECHO_SERVER}"


@pytest.mark.asyncio
async def test_echo_server_speaks_mcp_over_stdio() -> None:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=[str(ECHO_SERVER)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.server_info.name == "echo"

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            assert names == ["count_chars", "echo"]

            echoed = await session.call_tool("echo", {"text": "hello from the hub"})
            assert echoed.content[0].text == "hello from the hub"

            counted = await session.call_tool("count_chars", {"text": "abcd"})
            assert "4" in counted.content[0].text


@pytest.mark.asyncio
async def test_echo_server_reports_a_usable_input_schema() -> None:
    """Story 9.5 gates list calls on advertised capabilities and repairs schema
    defects; the fixture must be conformant so those tests fail for the right reason."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=[str(ECHO_SERVER)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()

    schema = next(t for t in tools.tools if t.name == "echo").input_schema
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict), "properties must be an object"
    assert "text" in schema["properties"]
