import json as _json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import log_capture
from ..child_manager import child_manager
from ..database import (
    ServerConfig,
    ServerType,
    add_server,
    delete_server,
    get_server,
    list_servers,
    update_server_enabled,
)
from ..installer import uninstall

router = APIRouter()


# ── Server management ────────────────────────────────────────────────────────

class AddServerRequest(BaseModel):
    name: str
    type: ServerType
    package: str
    args: list[str] = []
    env: dict[str, str] = {}


@router.get("/servers")
async def api_list_servers():
    servers = await list_servers()
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


@router.post("/servers", status_code=201)
async def api_add_server(req: AddServerRequest):
    try:
        config = await add_server(req.name, req.type, req.package, req.args, req.env)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        state = await child_manager.add(config)
        tools = [t.name for t in state.tools]
    except Exception as exc:
        # Saved to DB but failed to start – surface the error
        tools = []
        return {"server": _cfg(config), "tools": tools, "error": str(exc)}

    return {"server": _cfg(config), "tools": tools}


@router.delete("/servers/{server_id}")
async def api_delete_server(server_id: int):
    config = await get_server(server_id)
    if not config:
        raise HTTPException(status_code=404, detail="Server not found")
    await child_manager.remove(config.name)
    await uninstall(config)
    await delete_server(server_id)
    return {"deleted": server_id}


@router.post("/servers/{server_id}/enable")
async def api_enable_server(server_id: int):
    config = await get_server(server_id)
    if not config:
        raise HTTPException(status_code=404, detail="Server not found")
    await update_server_enabled(server_id, True)
    config.enabled = True
    try:
        state = await child_manager.add(config)
        return {"id": server_id, "enabled": True, "tool_count": len(state.tools)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/servers/{server_id}/disable")
async def api_disable_server(server_id: int):
    config = await get_server(server_id)
    if not config:
        raise HTTPException(status_code=404, detail="Server not found")
    await child_manager.remove(config.name)
    await update_server_enabled(server_id, False)
    return {"id": server_id, "enabled": False}


@router.post("/servers/{server_id}/restart")
async def api_restart_server(server_id: int):
    config = await get_server(server_id)
    if not config:
        raise HTTPException(status_code=404, detail="Server not found")
    try:
        state = await child_manager.restart(config.name)
        return {"id": server_id, "tool_count": len(state.tools)}
    except KeyError:
        raise HTTPException(status_code=404, detail="Server not running")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Tools ────────────────────────────────────────────────────────────────────

@router.get("/tools")
async def api_list_tools():
    return [
        {
            "server": name,
            "tool": tool.name,
            "description": tool.description,
            "inputSchema": tool.inputSchema,
        }
        for name, tool in child_manager.all_tools()
    ]


class CallToolRequest(BaseModel):
    server: str
    tool: str
    arguments: dict = {}


@router.post("/tools/call")
async def api_call_tool(req: CallToolRequest):
    state = child_manager.get(req.server)
    if not state:
        raise HTTPException(status_code=404, detail=f"Server '{req.server}' not found")
    if not state.running:
        raise HTTPException(status_code=503, detail=f"Server '{req.server}' is not running")
    try:
        result = await state.session.call_tool(req.tool, req.arguments)
        content = [
            c.model_dump() if hasattr(c, "model_dump") else {"type": "unknown", "raw": str(c)}
            for c in result.content
        ]
        return {
            "server": req.server,
            "tool": req.tool,
            "content": content,
            "isError": result.isError or False,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Logs ────────────────────────────────────────────────────────────────────

@router.get("/logs")
async def api_get_logs(server: str | None = None, limit: int = 200):
    return log_capture.get_entries(server=server, limit=limit)


@router.get("/logs/stream")
async def api_stream_logs(request: Request, server: str | None = None):
    from sse_starlette.sse import EventSourceResponse

    async def generator():
        async for entry in log_capture._broker.subscribe(server=server):
            yield {"data": _json.dumps(entry.as_dict())}

    return EventSourceResponse(generator())


@router.get("/logs/{server_name}/stderr")
async def api_get_stderr(server_name: str, limit: int = 200):
    lines = log_capture.read_log_file(server_name, limit=limit)
    return {"server": server_name, "lines": lines}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cfg(c: ServerConfig) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "type": c.type,
        "package": c.package,
        "args": c.args,
        "env": c.env,
        "enabled": c.enabled,
    }
