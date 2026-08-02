import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import admin_auth, log_capture, oauth
from .aggregator import mcp_server, sse_transport
from .api.oauth_router import router as oauth_router
from .api.routers import router as api_router
from .child_manager import child_manager
from .config import ADMIN_TOKEN, LOG_LEVEL
from .database import init_db, list_servers

logging.basicConfig(level=LOG_LEVEL)
log_capture.setup()
logger = logging.getLogger(__name__)


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

@app.get("/admin/login/github")
async def admin_login_github():
    return admin_auth.login_redirect()



@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse("/admin/login", status_code=302)
    response.delete_cookie("admin_session")
    return response


@app.get("/api/me")
async def api_me(request: Request):
    user = admin_auth.get_session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": user}
