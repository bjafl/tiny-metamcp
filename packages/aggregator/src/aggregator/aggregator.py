"""
MCP aggregator server.

Presents a single MCP endpoint that multiplexes tools from all running
child servers. Tools are namespaced as `<server>__<tool>` to avoid conflicts.
"""

from mcp import types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.sse import SseServerTransport

from . import meta_tools
from .child_manager import child_manager


async def handle_list_tools(
    _ctx: ServerRequestContext, _params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    tools = list(meta_tools.TOOLS)
    for server_name, tool in child_manager.all_tools():
        tools.append(
            types.Tool(
                name=f"{server_name}__{tool.name}",
                description=f"[{server_name}] {tool.description or ''}".strip(),
                inputSchema=tool.input_schema,
            )
        )
    return types.ListToolsResult(tools=tools)


async def handle_call_tool(
    _ctx: ServerRequestContext, params: types.CallToolRequestParams
) -> types.CallToolResult:
    if params.name in meta_tools.NAMES:
        content = await meta_tools.call(params.name, params.arguments or {})
        return types.CallToolResult(content=content, is_error=False)

    child, tool_name = child_manager.resolve(params.name)
    if child is None or not child.running:
        raise ValueError(f"No running server found for tool: {params.name!r}")
    result = await child.session.call_tool(tool_name, params.arguments or {})
    return types.CallToolResult(content=result.content, is_error=result.is_error or False)


mcp_server = Server(
    "mcp-aggregator",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)
sse_transport = SseServerTransport("/messages")
