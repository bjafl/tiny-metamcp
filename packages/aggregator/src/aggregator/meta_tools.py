"""
Native MCP tools for managing the aggregator's own server registry.

Unlike proxied tools (always namespaced `<server>__<tool>`), these use
plain names — there's no collision surface since proxied names always
contain `__` and these never do.
"""

import json

from mcp import types

from . import database
from .child_manager import child_manager
from .installer import uninstall
from .models import Server, ServerType


def _cfg(c: Server) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "type": c.type,
        "package": c.package,
        "args": c.get_args(),
        "env": c.get_env(),
        "enabled": c.enabled,
    }


async def _find_by_name(name: str) -> Server:
    for server in await database.list_servers():
        if server.name == name:
            return server
    raise ValueError(f"No server named {name!r}")


async def _list_servers(arguments: dict) -> list[dict]:
    servers = await database.list_servers()
    running = {s["name"]: s for s in child_manager.status()}
    return [
        {
            **_cfg(s),
            "running": running.get(s.name, {}).get("running", False),
            "tool_count": running.get(s.name, {}).get("tool_count", 0),
            "error": running.get(s.name, {}).get("error"),
        }
        for s in servers
    ]


async def _add_server(arguments: dict) -> dict:
    name = arguments["name"]
    type_str = arguments["type"]
    package = arguments["package"]
    args = arguments.get("args", [])
    env = arguments.get("env", {})

    server_type = ServerType(type_str)  # raises ValueError for an unknown type

    try:
        config = await database.add_server(name, server_type, package, args, env)
    except Exception as exc:
        raise ValueError(str(exc)) from exc

    try:
        state = await child_manager.add(config)
        tools = [t.name for t in state.tools]
        error = None
    except Exception as exc:
        tools = []
        error = str(exc)

    return {"server": _cfg(config), "tools": tools, "error": error}


async def _delete_server(arguments: dict) -> dict:
    name = arguments["name"]
    server = await _find_by_name(name)
    await child_manager.remove(server.name)
    await uninstall(server)
    await database.delete_server(server.id)
    return {"deleted": name}


async def _enable_server(arguments: dict) -> dict:
    name = arguments["name"]
    server = await _find_by_name(name)
    await database.update_server_enabled(server.id, True)
    server.enabled = True
    try:
        state = await child_manager.add(server)
        return {"name": name, "enabled": True, "tool_count": len(state.tools)}
    except Exception as exc:
        return {"name": name, "enabled": True, "tool_count": 0, "error": str(exc)}


async def _disable_server(arguments: dict) -> dict:
    name = arguments["name"]
    server = await _find_by_name(name)
    await child_manager.remove(server.name)
    await database.update_server_enabled(server.id, False)
    return {"name": name, "enabled": False}


async def _restart_server(arguments: dict) -> dict:
    name = arguments["name"]
    await _find_by_name(name)  # validates existence with a clear error first
    try:
        state = await child_manager.restart(name)
    except KeyError:
        raise ValueError(f"Server {name!r} is not running")
    return {"name": name, "tool_count": len(state.tools)}


_NAME_ONLY_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}

TOOLS: list[types.Tool] = [
    types.Tool(
        name="list_servers",
        description="List all configured MCP servers with their status.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="add_server",
        description="Add and start a new MCP server (pypi/npm/git/cmd).",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "type": {"type": "string"},
                "package": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}, "default": []},
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "default": {},
                },
            },
            "required": ["name", "type", "package"],
        },
    ),
    types.Tool(
        name="delete_server",
        description="Stop and permanently remove a configured MCP server.",
        inputSchema=_NAME_ONLY_SCHEMA,
    ),
    types.Tool(
        name="enable_server",
        description="Enable and start a previously disabled MCP server.",
        inputSchema=_NAME_ONLY_SCHEMA,
    ),
    types.Tool(
        name="disable_server",
        description="Stop and disable an MCP server without deleting it.",
        inputSchema=_NAME_ONLY_SCHEMA,
    ),
    types.Tool(
        name="restart_server",
        description="Restart a currently running MCP server.",
        inputSchema=_NAME_ONLY_SCHEMA,
    ),
]

NAMES: frozenset[str] = frozenset(t.name for t in TOOLS)

_HANDLERS = {
    "list_servers": _list_servers,
    "add_server": _add_server,
    "delete_server": _delete_server,
    "enable_server": _enable_server,
    "disable_server": _disable_server,
    "restart_server": _restart_server,
}


async def call(name: str, arguments: dict) -> list[types.TextContent]:
    result = await _HANDLERS[name](arguments)
    return [types.TextContent(type="text", text=json.dumps(result))]
