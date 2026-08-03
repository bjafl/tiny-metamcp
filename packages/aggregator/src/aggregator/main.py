import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import admin_auth, log_capture, oauth
from .aggregator import mcp_server, sse_transport
from .api.oauth_router import router as oauth_router
from .api.routers import router as api_router
from .child_manager import child_manager
from .config import ADMIN_TOKEN, LOG_LEVEL, WEBUI_DIST_DIR
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

    Called two ways: as a FastAPI Depends() on the /mcp route, and directly
    (not via Depends()) from _messages_asgi below, since /messages is a raw
    ASGI Mount that bypasses FastAPI's dependency injection. Keep both call
    sites in mind if this signature ever needs another Depends()-injected
    parameter -- the Depends() site would keep compiling silently while the
    manual site would need updating too.
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
#
# connect_sse() fully sends its own SSE response itself (per mcp.server.sse's
# module docstring); a FastAPI route handler that implicitly returns None
# makes FastAPI try to send a second response on top once the connection
# closes. Per the SDK's own documented example, returning a plain Response()
# fixes it — /mcp stays an ordinary FastAPI route (Depends()-based auth still
# applies), no ASGI-level restructuring needed here.
#
# /messages is different: handle_post_message() is a genuine raw ASGI
# callable (scope, receive, send) that must be mounted, not wrapped in a
# request/Response-style route — the SDK docstring mounts it directly via
# Mount(). A FastAPI-decorated route around it double-sends on *every* call,
# not just on close, since handle_post_message() sends its response
# synchronously within the call. Mount() bypasses FastAPI's route dispatch
# entirely (so Depends()-based auth doesn't run), hence the manual
# _check_bearer call here.


@app.get("/mcp")
async def mcp_sse(request: Request, _: None = Depends(_check_bearer)):
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as (read, write):
        await mcp_server.run(read, write, mcp_server.create_initialization_options())
    return Response()


async def _messages_asgi(scope, receive, send) -> None:
    if scope["type"] != "http":
        # This mount only ever serves POST /messages/; reject anything else
        # (e.g. a websocket handshake) before Request() asserts scope["type"]
        # == "http", which would otherwise surface as an unauthenticated
        # client generating a server-side AssertionError.
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        return
    request = Request(scope, receive)
    try:
        await _check_bearer(request)
    except HTTPException as exc:
        # Same shape FastAPI's default exception handler would have sent
        # for the old Depends()-based route.
        response = JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )
        await response(scope, receive, send)
        return
    await sse_transport.handle_post_message(scope, receive, send)


app.mount("/messages", _messages_asgi)


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


# ── SPA static serving ───────────────────────────────────────────────────────


@app.get("/admin")
@app.get("/admin/{path:path}")
async def admin_spa(path: str = ""):
    dist_root = WEBUI_DIST_DIR.resolve()
    candidate = (dist_root / path).resolve() if path else None
    if candidate and candidate.is_file() and dist_root in candidate.parents:
        return FileResponse(candidate)
    return FileResponse(dist_root / "index.html")
