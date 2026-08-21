import json as _json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import access_control, log_capture
from ..admin_auth import require_api_auth
from ..child_manager import child_manager
from ..database import (
    add_server,
    delete_server,
    get_server,
    update_server,
    update_server_enabled,
)
from ..installer import uninstall
from ..models import Server, ServerType, ServerVisibility

router = APIRouter(dependencies=[Depends(require_api_auth)])


# ── Server management ────────────────────────────────────────────────────────


class AddServerRequest(BaseModel):
    name: str
    type: ServerType
    package: str
    args: list[str] = []
    env: dict[str, str] = {}
    visibility: ServerVisibility = ServerVisibility.PRIVATE


class ServerUpdateRequest(BaseModel):
    name: str | None = None
    type: ServerType | None = None
    package: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    visibility: ServerVisibility | None = None


@router.get("/servers")
async def api_list_servers(username: str = Depends(require_api_auth)):
    servers = await access_control.visible_servers(username)
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
async def api_add_server(req: AddServerRequest, username: str = Depends(require_api_auth)):
    try:
        config = await add_server(
            req.name,
            req.type,
            req.package,
            req.args,
            req.env,
            owner_username=username,
            visibility=req.visibility.value,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        state = await child_manager.add(config)
        tools = [t.name for t in state.tools]
    except Exception as exc:
        tools = []
        return {"server": _cfg(config), "tools": tools, "error": str(exc)}

    return {"server": _cfg(config), "tools": tools}


@router.patch("/servers/{server_id}")
async def api_update_server(
    server_id: int, req: ServerUpdateRequest, username: str = Depends(require_api_auth)
):
    existing = await get_server(server_id)
    if not existing or not access_control.can_manage(existing, username):
        raise HTTPException(status_code=404, detail="Server not found")

    was_running = child_manager.get(existing.name) is not None
    try:
        config = await update_server(
            server_id,
            name=req.name,
            server_type=req.type,
            package=req.package,
            args=req.args,
            env=req.env,
            visibility=req.visibility.value if req.visibility is not None else None,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if config is None:
        raise HTTPException(status_code=404, detail="Server not found")

    nothing_changed = (
        existing.name == config.name
        and existing.type == config.type
        and existing.package == config.package
        and existing.args == config.args
        and existing.env == config.env
    )
    if nothing_changed:
        state = child_manager.get(config.name)
        return {
            **_cfg(config),
            "running": state.running if state else False,
            "tool_count": len(state.tools) if state else 0,
            "error": state.error if state else None,
        }

    renamed = existing.name != config.name
    if was_running and (renamed or not config.enabled):
        await child_manager.remove(existing.name)

    identity_changed = renamed or existing.type != config.type or existing.package != config.package
    if existing.type == ServerType.GIT.value and identity_changed:
        await uninstall(existing)

    if not config.enabled:
        return {**_cfg(config), "running": False, "tool_count": 0, "error": None}

    try:
        state = await child_manager.add(config)
        return {**_cfg(config), "running": True, "tool_count": len(state.tools), "error": None}
    except Exception as exc:
        return {**_cfg(config), "running": False, "tool_count": 0, "error": str(exc)}


@router.delete("/servers/{server_id}")
async def api_delete_server(server_id: int, username: str = Depends(require_api_auth)):
    config = await get_server(server_id)
    if not config or not access_control.can_manage(config, username):
        raise HTTPException(status_code=404, detail="Server not found")
    await child_manager.remove(config.name)
    await uninstall(config)
    await delete_server(server_id)
    return {"deleted": server_id}


@router.post("/servers/{server_id}/enable")
async def api_enable_server(server_id: int, username: str = Depends(require_api_auth)):
    config = await get_server(server_id)
    if not config or not access_control.can_manage(config, username):
        raise HTTPException(status_code=404, detail="Server not found")
    await update_server_enabled(server_id, True)
    config.enabled = True
    try:
        state = await child_manager.add(config)
        return {"id": server_id, "enabled": True, "tool_count": len(state.tools)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/servers/{server_id}/disable")
async def api_disable_server(server_id: int, username: str = Depends(require_api_auth)):
    config = await get_server(server_id)
    if not config or not access_control.can_manage(config, username):
        raise HTTPException(status_code=404, detail="Server not found")
    await child_manager.remove(config.name)
    await update_server_enabled(server_id, False)
    return {"id": server_id, "enabled": False}


@router.post("/servers/{server_id}/restart")
async def api_restart_server(server_id: int, username: str = Depends(require_api_auth)):
    config = await get_server(server_id)
    if not config or not access_control.can_manage(config, username):
        raise HTTPException(status_code=404, detail="Server not found")
    try:
        state = await child_manager.restart(config.name)
        return {"id": server_id, "tool_count": len(state.tools)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Server not running") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── Tools ────────────────────────────────────────────────────────────────────


@router.get("/tools")
async def api_list_tools(username: str = Depends(require_api_auth)):
    visible = await access_control.visible_server_names(username)
    return [
        {
            "server": name,
            "tool": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
        }
        for name, tool in child_manager.all_tools()
        if name in visible
    ]


class CallToolRequest(BaseModel):
    server: str
    tool: str
    arguments: dict = {}


@router.post("/tools/call")
async def api_call_tool(req: CallToolRequest, username: str = Depends(require_api_auth)):
    if req.server not in await access_control.visible_server_names(username):
        raise HTTPException(status_code=404, detail=f"Server '{req.server}' not found")
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
            "isError": result.is_error or False,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── Logs ────────────────────────────────────────────────────────────────────


@router.get("/logs")
async def api_get_logs(
    server: str | None = None, limit: int = 200, username: str = Depends(require_api_auth)
):
    visible = await access_control.visible_server_names(username)
    if server:
        if server not in visible:
            raise HTTPException(status_code=404, detail=f"Server '{server}' not found")
        return log_capture.get_entries(server=server, limit=limit)
    return [
        e for e in log_capture.get_entries(limit=limit) if not e["server"] or e["server"] in visible
    ]


@router.get("/logs/stream")
async def api_stream_logs(
    request: Request, server: str | None = None, username: str = Depends(require_api_auth)
):
    from sse_starlette.sse import EventSourceResponse

    visible = await access_control.visible_server_names(username)
    if server and server not in visible:
        raise HTTPException(status_code=404, detail=f"Server '{server}' not found")

    async def generator():
        # log_capture._broker.subscribe(server=X) already only yields
        # entries for X when X is given, so the visibility check above
        # already fully scoped that case -- this per-entry filter only
        # matters for the server=None (all-servers) case.
        async for entry in log_capture._broker.subscribe(server=server):
            if server or not entry.server or entry.server in visible:
                yield {"data": _json.dumps(entry.as_dict())}

    return EventSourceResponse(generator())


@router.get("/logs/{server_name}/stderr")
async def api_get_stderr(
    server_name: str, limit: int = 200, username: str = Depends(require_api_auth)
):
    if server_name not in await access_control.visible_server_names(username):
        raise HTTPException(status_code=404, detail=f"Server '{server_name}' not found")
    lines = log_capture.read_log_file(server_name, limit=limit)
    return {"server": server_name, "lines": lines}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _cfg(c: Server) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "type": c.type,
        "package": c.package,
        "args": c.get_args(),
        "env": c.get_env(),
        "enabled": c.enabled,
        "owner": c.owner_username,
        "visibility": c.visibility,
    }
