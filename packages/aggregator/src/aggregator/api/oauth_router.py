"""OAuth 2.1 endpoints — included at root (no prefix)."""

import html
import urllib.parse

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import admin_auth, identity_providers, oauth

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


def _provider_choice_page(state: str) -> HTMLResponse:
    links = "".join(
        f'<p><a href="/authorize/continue?provider={html.escape(p.slug)}'
        f'&state={urllib.parse.quote(state)}">Continue with {html.escape(p.slug.capitalize())}</a></p>'
        for p in identity_providers.configured_providers()
    )
    return HTMLResponse(f"<h1>Choose a login method</h1>{links}")


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

    session_state = oauth.start_session(client_id, redirect_uri, code_challenge, state)
    configured = identity_providers.configured_providers()
    if not configured:
        return JSONResponse(
            {"error": "server_error", "error_description": "No identity provider configured"},
            status_code=500,
        )
    if len(configured) == 1:
        return configured[0].login_redirect(session_state)
    return _provider_choice_page(session_state)


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


@router.get("/authorize/continue")
async def authorize_continue(provider: str = Query(...), state: str = Query(...)):
    """Reached from the provider-choice page (only rendered when more than
    one provider is configured) -- forwards the pending PKCE session's
    state to whichever provider the user picked."""
    p = identity_providers.get_provider(provider)
    if p is None or not p.is_configured():
        return JSONResponse(
            {"error": "invalid_request", "error_description": "unknown or unconfigured provider"},
            status_code=400,
        )
    return p.login_redirect(state)


# ── Provider callbacks ──────────────────────────────────────────────────────────
# Both the admin browser flow and the MCP OAuth flow redirect here. The
# admin flow sets an `admin_oauth_state` cookie before leaving for the
# provider, which serves as the discriminator. GitHub's callback path is
# unchanged (`/oauth/callback`) so existing OAuth App registrations don't
# need updating; Steam gets its own new path since its callback params are
# shaped completely differently (openid.* instead of code/state).


async def _handle_oauth_callback(request: Request, provider: identity_providers.IdentityProvider):
    if request.cookies.get("admin_oauth_state"):
        return await admin_auth.handle_callback(request, provider)

    state = request.query_params.get("state")
    if not state:
        return HTMLResponse("<h1>OAuth error</h1><p>missing_state</p>", status_code=400)

    result = await provider.resolve_callback(request)
    if result is None:
        return HTMLResponse(
            "<h1>Access denied</h1><p>Authentication failed or user is not authorized.</p>",
            status_code=403,
        )

    finish_result = await oauth.finish_session(state, result.username)
    if not finish_result:
        return HTMLResponse(
            "<h1>Access denied</h1><p>Authentication failed or user is not authorized.</p>",
            status_code=403,
        )

    auth_code, redirect_uri, client_state = finish_result
    qs = urllib.parse.urlencode({"code": auth_code, "state": client_state})
    return RedirectResponse(f"{redirect_uri}?{qs}", status_code=302)


@router.get("/oauth/callback")
async def oauth_callback(request: Request):
    return await _handle_oauth_callback(request, identity_providers.github_provider)


@router.get("/oauth/callback/steam")
async def oauth_callback_steam(request: Request):
    return await _handle_oauth_callback(request, identity_providers.steam_provider)


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
