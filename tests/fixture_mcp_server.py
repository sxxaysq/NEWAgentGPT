"""An independently launched MCP server used by the external-adapter test."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("external-fixture")


@server.tool()
def echo(text: str) -> str:
    """Return the supplied text, for integration testing."""
    return "external:" + text


if __name__ == "__main__":
    server.run(transport="stdio")
