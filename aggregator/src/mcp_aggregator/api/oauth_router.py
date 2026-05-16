"""OAuth 2.1 endpoints — included at root (no prefix)."""

import urllib.parse

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import oauth
from ..config import GITHUB_CLIENT_ID, MCP_DOMAIN

router = APIRouter()


# ── Discovery ─────────────────────────────────────────────────────────────────

@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    return oauth.protected_resource_metadata()


@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server():
    return oauth.authorization_server_metadata()


# ── Authorization request ─────────────────────────────────────────────────────

@router.get("/oauth/authorize")
async def oauth_authorize(
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    state: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query(...),
    scope: str = Query(default="mcp"),
):
    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
    if code_challenge_method != "S256":
        return JSONResponse({"error": "invalid_request", "error_description": "Only S256 supported"}, status_code=400)
    if not await oauth.validate_client(client_id, redirect_uri):
        return JSONResponse({"error": "invalid_client"}, status_code=400)

    github_state = oauth.start_session(client_id, redirect_uri, code_challenge, state)

    params = urllib.parse.urlencode({
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": f"https://{MCP_DOMAIN}/oauth/callback",
        "scope": "read:user",
        "state": github_state,
    })
    return RedirectResponse(
        f"https://github.com/login/oauth/authorize?{params}", status_code=302
    )


# ── GitHub callback ───────────────────────────────────────────────────────────

@router.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    error_description: str = Query(None),
):
    if error or not code or not state:
        msg = error_description or error or "missing_code"
        return HTMLResponse(
            f"<h1>OAuth-feil</h1><p>{msg}</p>", status_code=400
        )

    result = await oauth.finish_session(code, state)
    if not result:
        return HTMLResponse(
            "<h1>Tilgang nektet</h1>"
            "<p>Autentisering feilet eller brukeren er ikke autorisert.</p>",
            status_code=403,
        )

    auth_code, redirect_uri, client_state = result
    qs = urllib.parse.urlencode({"code": auth_code, "state": client_state})
    return RedirectResponse(f"{redirect_uri}?{qs}", status_code=302)


# ── Token endpoint ────────────────────────────────────────────────────────────

@router.post("/oauth/token")
async def oauth_token(
    grant_type: str = Form(...),
    code: str = Form(None),
    code_verifier: str = Form(None),
    client_id: str = Form(None),
    redirect_uri: str = Form(None),
    refresh_token: str = Form(None),
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
