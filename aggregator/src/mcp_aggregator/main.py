import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import admin_auth, log_capture, oauth
from .aggregator import mcp_server, sse_transport
from .api.oauth_router import router as oauth_router
from .api.routers import router as api_router
from .child_manager import child_manager
from .config import ADMIN_TOKEN, LOG_LEVEL
from .database import (
    add_server,
    delete_server,
    get_server,
    init_db,
    list_servers,
    update_server_enabled,
)
from .installer import uninstall
from .models import ServerType

logging.basicConfig(level=LOG_LEVEL)
log_capture.setup()
logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


async def _token_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            await oauth.cleanup_expired()
        except Exception as exc:
            logger.warning("Token cleanup error: %s", exc)


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
    cleanup_task = asyncio.create_task(_token_cleanup_loop())
    yield
    cleanup_task.cancel()
    for name in list(child_manager._children):
        await child_manager.remove(name)


app = FastAPI(title="MCP Aggregator", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(oauth_router)
app.include_router(api_router, prefix="/api")


# ── Bearer auth for MCP endpoints ────────────────────────────────────────────

async def _check_bearer(request: Request) -> None:
    """
    Bearer auth for /mcp and /messages.
    Accepts static ADMIN_TOKEN or a valid OAuth access token.
    Returns 401 + WWW-Authenticate so MCP clients can discover OAuth.
    """
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        if ADMIN_TOKEN and token == ADMIN_TOKEN:
            return
        if await oauth.validate_bearer(token):
            return
    raise HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": oauth.www_authenticate_header()},
    )


# ── MCP SSE transport ─────────────────────────────────────────────────────────

@app.get("/mcp")
async def mcp_sse(request: Request, _: None = Depends(_check_bearer)):
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as (read, write):
        await mcp_server.run(read, write, mcp_server.create_initialization_options())


@app.post("/messages")
async def mcp_messages(request: Request, _: None = Depends(_check_bearer)):
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "servers": child_manager.status()}


# ── Admin auth routes (no session required) ───────────────────────────────────

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request, error: str = ""):
    return templates.TemplateResponse(
        "admin/login.html", {"request": request, "error": error}
    )


@app.get("/admin/login/github")
async def admin_login_github():
    return admin_auth.login_redirect()



@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse("/admin/login", status_code=302)
    response.delete_cookie("admin_session")
    return response


# ── Admin UI (session required) ───────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_root(request: Request, user: str = Depends(admin_auth.require_admin)):
    return templates.TemplateResponse("admin/index.html", {"request": request, "user": user})


async def _render_servers_table(request: Request) -> HTMLResponse:
    servers = await list_servers()
    running_map = {s["name"]: s for s in child_manager.status()}
    enriched = []
    for s in servers:
        st = running_map.get(s.name, {})
        enriched.append({
            "id": s.id,
            "name": s.name,
            "type": s.type,
            "package": s.package,
            "enabled": s.enabled,
            "running": st.get("running", False),
            "tool_count": st.get("tool_count", 0),
            "error": st.get("error"),
        })
    return templates.TemplateResponse(
        "admin/_servers_table.html", {"request": request, "servers": enriched}
    )


@app.get("/admin/servers-table", response_class=HTMLResponse)
async def admin_servers_table(
    request: Request, _: str = Depends(admin_auth.require_admin)
):
    return await _render_servers_table(request)


@app.post("/admin/add-server", response_class=HTMLResponse)
async def admin_add_server(
    request: Request, _: str = Depends(admin_auth.require_admin)
):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    type_ = str(form.get("type", "pypi"))
    package = str(form.get("package", "")).strip()
    raw_args = str(form.get("args", "")).strip()
    raw_env = str(form.get("env", "")).strip()

    args = [a.strip() for a in raw_args.split(",") if a.strip()] if raw_args else []
    env: dict[str, str] = {}
    for pair in (raw_env.split(",") if raw_env else []):
        if "=" in pair:
            k, v = pair.split("=", 1)
            env[k.strip()] = v.strip()

    try:
        config = await add_server(name, ServerType(type_), package, args, env)
    except Exception as exc:
        return templates.TemplateResponse(
            "admin/_add_result.html",
            {"request": request, "name": name, "tools": [], "error": str(exc)},
        )

    try:
        state = await child_manager.add(config)
        tool_names = [t.name for t in state.tools]
        error = None
    except Exception as exc:
        tool_names = []
        error = str(exc)

    return templates.TemplateResponse(
        "admin/_add_result.html",
        {"request": request, "name": config.name, "tools": tool_names, "error": error},
    )


@app.post("/admin/servers/{server_id}/enable", response_class=HTMLResponse)
async def admin_enable(
    server_id: int, request: Request, _: str = Depends(admin_auth.require_admin)
):
    config = await get_server(server_id)
    if not config:
        raise HTTPException(404)
    await update_server_enabled(server_id, True)
    config.enabled = True
    try:
        await child_manager.add(config)
    except Exception as exc:
        logger.warning("Enable %s failed: %s", config.name, exc)
    return await _render_servers_table(request)


@app.post("/admin/servers/{server_id}/disable", response_class=HTMLResponse)
async def admin_disable(
    server_id: int, request: Request, _: str = Depends(admin_auth.require_admin)
):
    config = await get_server(server_id)
    if not config:
        raise HTTPException(404)
    await child_manager.remove(config.name)
    await update_server_enabled(server_id, False)
    return await _render_servers_table(request)


@app.post("/admin/servers/{server_id}/restart", response_class=HTMLResponse)
async def admin_restart(
    server_id: int, request: Request, _: str = Depends(admin_auth.require_admin)
):
    config = await get_server(server_id)
    if not config:
        raise HTTPException(404)
    try:
        await child_manager.restart(config.name)
    except KeyError:
        raise HTTPException(404, "Server not running")
    return await _render_servers_table(request)


@app.delete("/admin/servers/{server_id}", response_class=HTMLResponse)
async def admin_delete(
    server_id: int, request: Request, _: str = Depends(admin_auth.require_admin)
):
    config = await get_server(server_id)
    if not config:
        raise HTTPException(404)
    await child_manager.remove(config.name)
    await uninstall(config)
    await delete_server(server_id)
    return await _render_servers_table(request)
