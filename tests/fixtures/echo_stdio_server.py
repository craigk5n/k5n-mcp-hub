"""A minimal stdio MCP server, used to test the hub against a real subprocess.

Epic 9 (stdio transport) is tested against this rather than against mocks. It is
intentionally trivial and dependency-free: it uses only the `mcp` package already
pinned in `pyproject.toml`, so CI's clean-install gate keeps meaning what it says.

`MCPServer` is `mcp` 2.x's name for what 1.x called `FastMCP`; importing
`mcp.server.fastmcp` here raises a ModuleNotFoundError that says so. `run()`
already defaults to `transport="stdio"`, so no transport argument is needed.

Run it directly to poke at it by hand:

    python3 tests/fixtures/echo_stdio_server.py
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("echo")


@mcp.tool(description="Return the text you were given.")
def echo(text: str) -> str:
    return text


@mcp.tool(description="Report how many characters the text has.")
def count_chars(text: str) -> int:
    return len(text)


if __name__ == "__main__":
    mcp.run()
