import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .aggregator import mcp_server, sse_transport
from .api.routers import router as api_router
from .child_manager import child_manager
from .config import ADMIN_TOKEN, LOG_LEVEL
from .database import ServerType, add_server, init_db, list_servers, update_server_enabled
from .ui import ADMIN_HTML, add_result_html, servers_table_html

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    servers = await list_servers()
    for srv in servers:
        if srv.enabled:
            try:
                await child_manager.add(srv)
            except Exception as exc:
                logger.error("Could not start %s on boot: %s", srv.name, exc)
    yield
    for name in list(child_manager._children):
        await child_manager.remove(name)


app = FastAPI(title="MCP Aggregator", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(api_router, prefix="/api")

# ── Auth helpers ─────────────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


def _require_token(
    creds: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    if not ADMIN_TOKEN:
        return
    if creds is None or creds.credentials != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── MCP SSE transport ─────────────────────────────────────────────────────────

@app.get("/mcp")
async def mcp_sse(request: Request):
    """MCP SSE endpoint – protected by mcp-auth-proxy upstream."""
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as (read, write):
        await mcp_server.run(read, write, mcp_server.create_initialization_options())


@app.post("/messages")
async def mcp_messages(request: Request):
    """MCP SSE message post-back endpoint."""
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "servers": child_manager.status(),
    }


# ── Admin UI (HTMX) ───────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(_require_token)])
async def admin_root():
    return ADMIN_HTML


@app.get("/admin/servers-table", response_class=HTMLResponse, dependencies=[Depends(_require_token)])
async def admin_servers_table():
    servers = await list_servers()
    running_map = {s["name"]: s for s in child_manager.status()}
    enriched = [
        {
            **{"id": s.id, "name": s.name, "type": s.type, "package": s.package,
               "enabled": s.enabled},
            **running_map.get(s.name, {"running": False, "tool_count": 0, "error": None}),
        }
        for s in servers
    ]
    return servers_table_html(enriched)


@app.post("/admin/add-server", response_class=HTMLResponse, dependencies=[Depends(_require_token)])
async def admin_add_server(request: Request):
    form = await request.form()
    name = form.get("name", "").strip()
    type_ = form.get("type", "pypi")
    package = form.get("package", "").strip()
    raw_args = form.get("args", "").strip()
    raw_env = form.get("env", "").strip()

    args = [a.strip() for a in raw_args.split(",") if a.strip()] if raw_args else []
    env: dict[str, str] = {}
    for pair in (raw_env.split(",") if raw_env else []):
        if "=" in pair:
            k, v = pair.split("=", 1)
            env[k.strip()] = v.strip()

    try:
        config = await add_server(name, ServerType(type_), package, args, env)
    except Exception as exc:
        return add_result_html({}, [], str(exc))

    try:
        state = await child_manager.add(config)
        tools = [t.name for t in state.tools]
        return add_result_html(
            {"name": config.name}, tools, None
        )
    except Exception as exc:
        return add_result_html({"name": config.name}, [], str(exc))


@app.post("/admin/servers/{server_id}/enable", response_class=HTMLResponse, dependencies=[Depends(_require_token)])
async def admin_enable(server_id: int):
    from .database import get_server
    config = await get_server(server_id)
    if not config:
        raise HTTPException(404)
    await update_server_enabled(server_id, True)
    config.enabled = True
    await child_manager.add(config)
    return ""


@app.post("/admin/servers/{server_id}/disable", response_class=HTMLResponse, dependencies=[Depends(_require_token)])
async def admin_disable(server_id: int):
    from .database import get_server
    config = await get_server(server_id)
    if not config:
        raise HTTPException(404)
    await child_manager.remove(config.name)
    await update_server_enabled(server_id, False)
    return ""


@app.post("/admin/servers/{server_id}/restart", response_class=HTMLResponse, dependencies=[Depends(_require_token)])
async def admin_restart(server_id: int):
    from .database import get_server
    config = await get_server(server_id)
    if not config:
        raise HTTPException(404)
    await child_manager.restart(config.name)
    return ""


@app.delete("/admin/servers/{server_id}", response_class=HTMLResponse, dependencies=[Depends(_require_token)])
async def admin_delete(server_id: int):
    from .database import get_server
    from .installer import uninstall
    from .database import delete_server
    config = await get_server(server_id)
    if not config:
        raise HTTPException(404)
    await child_manager.remove(config.name)
    await uninstall(config)
    await delete_server(server_id)
    return ""
