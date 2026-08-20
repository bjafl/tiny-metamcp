"""
MCP aggregator server.

Presents a single MCP endpoint that multiplexes tools from all running
child servers. Tools are namespaced as `<server>__<tool>` to avoid conflicts.
"""

from contextvars import ContextVar

from mcp import types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from . import access_control, meta_tools
from .child_manager import child_manager

# Set by main.py's bearer-auth check, once per /mcp request, before handing
# off to mcp_server.run()/streamable_manager.handle_request(). mcp_server
# below is one shared, stateless Server instance reused across every
# concurrent /mcp connection -- there's no per-connection object to hang
# per-request identity off of. A ContextVar is the right tool here (not a
# plain module variable): each request runs in its own asyncio task, and
# asyncio copies the context into new tasks, so this stays correctly
# isolated per concurrent connection even though mcp_server itself is
# long-lived and shared.
current_user: ContextVar[str] = ContextVar("current_user")


async def handle_list_tools(
    _ctx: ServerRequestContext, _params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    username = current_user.get()
    visible = await access_control.visible_server_names(username)
    tools = list(meta_tools.TOOLS)
    for server_name, tool in child_manager.all_tools():
        if server_name not in visible:
            continue
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
    username = current_user.get()
    if params.name in meta_tools.NAMES:
        content = await meta_tools.call(params.name, params.arguments or {}, username)
        return types.CallToolResult(content=content, is_error=False)

    child, tool_name = child_manager.resolve(params.name)
    if child is None or not child.running:
        raise ValueError(f"No running server found for tool: {params.name!r}")
    if child.config.name not in await access_control.visible_server_names(username):
        raise ValueError(f"No running server found for tool: {params.name!r}")
    result = await child.session.call_tool(tool_name, params.arguments or {})
    return types.CallToolResult(content=result.content, is_error=result.is_error or False)


mcp_server = Server(
    "mcp-aggregator",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)
sse_transport = SseServerTransport("/messages/")

# Modern Streamable HTTP transport (single endpoint, POST-first), mounted
# alongside the legacy SSE transport above -- both wrap the same mcp_server,
# which is stateless/reentrant across concurrent connections by design.
streamable_manager = StreamableHTTPSessionManager(mcp_server, stateless=False)
