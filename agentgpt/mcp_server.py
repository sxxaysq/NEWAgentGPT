"""Built-in tools are served over real MCP stdio, exactly like external tools."""

import asyncio
import json

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import Server

from agentgpt.config import settings
from agentgpt.store import Store
from agentgpt.tools import builtin_tools


async def main():
    store = Store(settings.database)
    tools = {tool.name: tool for tool in builtin_tools(settings, store)}
    server = Server("agentgpt-builtin")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.input_schema,
                annotations=types.ToolAnnotations(
                    readOnlyHint=tool.read_only,
                    destructiveHint=not tool.read_only,
                ),
            )
            for tool in tools.values()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        tool = tools.get(name)
        if tool is None:
            raise ValueError("Unknown tool: " + name)
        try:
            result = await tool.invoke(arguments)
        except Exception as exc:
            # TimeoutError has an empty str(); preserve its type for agent recovery.
            raise ValueError(
                f"{type(exc).__name__}: {str(exc)[:500] or 'Tool request timed out'}"
            ) from exc
        return [
            types.TextContent(
                type="text",
                text=result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
            )
        ]

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
