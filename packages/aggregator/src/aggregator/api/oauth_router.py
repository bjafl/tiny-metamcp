"""OAuth 2.1 endpoints — included at root (no prefix)."""

import html
import urllib.parse

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import admin_auth, oauth
from ..config import GITHUB_CLIENT_ID, MCP_DOMAIN

router = APIRouter()


# ── Discovery ─────────────────────────────────────────────────────────────────


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    return oauth.protected_resource_metadata()


@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server():
    return oauth.authorization_server_metadata()


# ── Dynamic Client Registration (RFC7591) ─────────────────────────────────────


@router.post("/register")
async def oauth_register(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    redirect_uris = body.get("redirect_uris")
    if not redirect_uris or not isinstance(redirect_uris, list):
        return JSONResponse(
            {"error": "invalid_redirect_uri", "error_description": "redirect_uris required"},
            status_code=400,
        )

    client_id = body.get("client_id", "")
    registration = oauth.register_client(client_id, redirect_uris)
    return JSONResponse(registration, status_code=201)


# ── Authorization request ─────────────────────────────────────────────────────


async def _authorize_handler(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    scope: str,
):
    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
    if code_challenge_method != "S256":
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Only S256 supported"},
            status_code=400,
        )
    if not await oauth.validate_client(client_id, redirect_uri):
        return JSONResponse({"error": "invalid_client"}, status_code=400)

    github_state = oauth.start_session(client_id, redirect_uri, code_challenge, state)
    params = urllib.parse.urlencode(
        {
            "client_id": GITHUB_CLIENT_ID,
            "redirect_uri": f"https://{MCP_DOMAIN}/oauth/callback",
            "scope": "read:user",
            "state": github_state,
        }
    )
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{params}", status_code=302)


@router.get("/authorize")
async def oauth_authorize(
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    state: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query(...),
    scope: str = Query(default="mcp"),
):
    return await _authorize_handler(
        response_type,
        client_id,
        redirect_uri,
        state,
        code_challenge,
        code_challenge_method,
        scope,
    )


# Alias kept for clients that cached old discovery documents
@router.get("/oauth/authorize")
async def oauth_authorize_alias(
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    state: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query(...),
    scope: str = Query(default="mcp"),
):
    return await _authorize_handler(
        response_type,
        client_id,
        redirect_uri,
        state,
        code_challenge,
        code_challenge_method,
        scope,
    )


# ── Shared GitHub callback ────────────────────────────────────────────────────
# Both the admin browser flow and the MCP OAuth flow redirect here.
# The admin flow sets an `admin_oauth_state` cookie before going to GitHub,
# which serves as the discriminator.


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    error_description: str = Query(None),
):
    if request.cookies.get("admin_oauth_state"):
        return await admin_auth.handle_callback(request)

    if error or not code or not state:
        msg = html.escape(error_description or error or "missing_code")
        return HTMLResponse(f"<h1>OAuth error</h1><p>{msg}</p>", status_code=400)

    result = await oauth.finish_session(code, state)
    if not result:
        return HTMLResponse(
            "<h1>Access denied</h1><p>Authentication failed or user is not authorized.</p>",
            status_code=403,
        )

    auth_code, redirect_uri, client_state = result
    qs = urllib.parse.urlencode({"code": auth_code, "state": client_state})
    return RedirectResponse(f"{redirect_uri}?{qs}", status_code=302)


# ── Token endpoint ────────────────────────────────────────────────────────────


async def _token_handler(
    grant_type: str,
    code: str | None,
    code_verifier: str | None,
    client_id: str | None,
    redirect_uri: str | None,
    refresh_token: str | None,
):
    if grant_type == "authorization_code":
        if not all([code, code_verifier, client_id, redirect_uri]):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        result = await oauth.exchange_code(code, code_verifier, client_id, redirect_uri)
        if not result:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access, new_refresh = result
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": oauth.ACCESS_TOKEN_TTL,
            "refresh_token": new_refresh,
            "scope": "mcp",
        }

    if grant_type == "refresh_token":
        if not refresh_token:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        result = await oauth.rotate_refresh(refresh_token)
        if not result:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access, new_refresh = result
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": oauth.ACCESS_TOKEN_TTL,
            "refresh_token": new_refresh,
            "scope": "mcp",
        }

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


@router.post("/token")
async def oauth_token(
    grant_type: str = Form(...),
    code: str = Form(None),
    code_verifier: str = Form(None),
    client_id: str = Form(None),
    redirect_uri: str = Form(None),
    refresh_token: str = Form(None),
):
    return await _token_handler(
        grant_type, code, code_verifier, client_id, redirect_uri, refresh_token
    )


# Alias kept for clients that cached old discovery documents
@router.post("/oauth/token")
async def oauth_token_alias(
    grant_type: str = Form(...),
    code: str = Form(None),
    code_verifier: str = Form(None),
    client_id: str = Form(None),
    redirect_uri: str = Form(None),
    refresh_token: str = Form(None),
):
    return await _token_handler(
        grant_type, code, code_verifier, client_id, redirect_uri, refresh_token
    )
