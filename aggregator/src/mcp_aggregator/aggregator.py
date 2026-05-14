"""
MCP aggregator server.

Presents a single MCP endpoint that multiplexes tools from all running
child servers. Tools are namespaced as `<server>__<tool>` to avoid conflicts.
"""

import mcp.types as types
from mcp.server import Server
from mcp.server.sse import SseServerTransport

from .child_manager import child_manager

mcp_server = Server("mcp-aggregator")
sse_transport = SseServerTransport("/messages")


@mcp_server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    tools = []
    for server_name, tool in child_manager.all_tools():
        tools.append(
            types.Tool(
                name=f"{server_name}__{tool.name}",
                description=f"[{server_name}] {tool.description or ''}".strip(),
                inputSchema=tool.inputSchema,
            )
        )
    return tools


@mcp_server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    child, tool_name = child_manager.resolve(name)
    if child is None or not child.running:
        raise ValueError(f"No running server found for tool: {name!r}")
    result = await child.session.call_tool(tool_name, arguments or {})
    return result.content
