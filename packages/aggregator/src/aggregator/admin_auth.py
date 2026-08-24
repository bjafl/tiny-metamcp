"""
Admin browser session auth: any configured IdentityProvider + itsdangerous
signed cookies.

Separate from the MCP OAuth 2.1 flow (oauth.py) which serves AI clients.
Both flows share the same IdentityProvider instances (identity_providers.py)
but use different callback-disambiguation paths.
"""

import logging
import secrets
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import access_control
from .config import SESSION_SECRET
from .identity_providers import IdentityProvider

logger = logging.getLogger(__name__)

_signer = URLSafeTimedSerializer(SESSION_SECRET, salt="admin-session")
_state_signer = URLSafeTimedSerializer(SESSION_SECRET, salt="admin-oauth-state")
_link_state_signer = URLSafeTimedSerializer(SESSION_SECRET, salt="admin-link-state")

SESSION_MAX_AGE = 7 * 86_400  # 7 days
STATE_MAX_AGE = 600  # 10 minutes


def _load_session_payload(request: Request) -> dict | None:
    cookie = request.cookies.get("admin_session")
    if not cookie:
        return None
    try:
        payload = _signer.loads(cookie, max_age=SESSION_MAX_AGE)
    except BadSignature, SignatureExpired:
        return None
    # Sessions issued before multi-provider support signed a plain string,
    # not a {"username", "display_name"} dict -- treat as invalid rather
    # than crash, forcing a one-time re-login.
    if not isinstance(payload, dict):
        return None
    return payload


async def get_session_user(request: Request) -> str | None:
    """Return the authenticated (canonical "user:<id>") username from the
    session cookie, or None."""
    payload = _load_session_payload(request)
    if payload is None:
        return None
    username = payload.get("username")
    if not username or not await access_control.is_session_valid(username):
        return None
    return username


def get_session_display_name(request: Request) -> str | None:
    """Return the cosmetic display name from the session cookie, or None.
    Never used for access control -- only get_session_user()'s return
    value is."""
    payload = _load_session_payload(request)
    if payload is None:
        return None
    return payload.get("display_name")


async def require_api_auth(request: Request) -> str:
    """FastAPI dependency for /api/* routes.

    Accepts an admin session cookie (browser) or a personal-token Bearer
    header (programmatic access). Does not accept MCP OAuth tokens — those
    are only valid for /mcp and /messages. Returns the authenticated
    username so callers can scope their query to it.
    """
    user = await get_session_user(request)
    if user:
        return user
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        username = await access_control.validate_personal_token(auth[7:])
        if username:
            return username
    raise HTTPException(status_code=401, detail="Unauthorized")


async def require_admin(request: Request) -> str:
    """FastAPI dependency for admin-only /api/* routes (see
    api/users_router.py). Same acceptance as require_api_auth, plus an
    admin-rights check."""
    user = await require_api_auth(request)
    if not await access_control.is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def login_redirect(provider: IdentityProvider) -> RedirectResponse:
    """Start the given identity provider's login flow for the admin
    browser session."""
    state = secrets.token_urlsafe(32)
    state_token = _state_signer.dumps(state)
    response = provider.login_redirect(state)
    response.set_cookie(
        "admin_oauth_state",
        state_token,
        httponly=True,
        max_age=STATE_MAX_AGE,
        samesite="lax",
    )
    return response


def _login_error(msg: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/login?error={urllib.parse.quote(msg)}", status_code=302)


async def handle_callback(request: Request, provider: IdentityProvider) -> RedirectResponse:
    """Handle an identity provider's callback for the admin browser flow,
    set session cookie, redirect to /admin."""
    state_token = request.cookies.get("admin_oauth_state")
    if not state_token:
        return _login_error("Missing state cookie — possible CSRF")
    try:
        stored_state = _state_signer.loads(state_token, max_age=STATE_MAX_AGE)
    except BadSignature, SignatureExpired:
        return _login_error("Invalid or expired state — please try again")
    request_state = request.query_params.get("state")
    if not request_state or not secrets.compare_digest(request_state, stored_state):
        return _login_error("State mismatch — please try again")

    result = await provider.resolve_callback(request)
    if result is None:
        return _login_error("Authentication error — please try again")

    provider_slug, _, raw_id = result.username.partition(":")
    canonical = await access_control.resolve_login(provider_slug, raw_id, result.display_name)
    if canonical is None:
        logger.warning("Admin login denied: %s not allowed", result.username)
        return _login_error(f"User '{result.username}' is not authorized")

    logger.info("Admin login: %s (%s)", canonical, result.username)
    session_value = _signer.dumps({"username": canonical, "display_name": result.display_name})
    response = RedirectResponse("/admin", status_code=302)
    response.set_cookie(
        "admin_session",
        session_value,
        httponly=True,
        max_age=SESSION_MAX_AGE,
        secure=True,
        samesite="lax",
    )
    response.delete_cookie("admin_oauth_state")
    return response


async def login_redirect_for_link(request: Request, provider: IdentityProvider) -> RedirectResponse:
    """Start `provider`'s login flow to link a new identity onto the
    CURRENTLY authenticated session's account. Raises 401 if there's no
    valid session -- this route must never accept a forged/anonymous
    "link to user X" request. The account to link onto is read from the
    server-signed session cookie, then re-signed into the state cookie
    below -- it can't be supplied or tampered with by the client."""
    current_user = await get_session_user(request)
    if current_user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    state = secrets.token_urlsafe(32)
    state_token = _link_state_signer.dumps({"state": state, "user": current_user})
    response = provider.login_redirect(state)
    response.set_cookie(
        "link_identity_state",
        state_token,
        httponly=True,
        max_age=STATE_MAX_AGE,
        samesite="lax",
    )
    return response


def _link_error(msg: str) -> RedirectResponse:
    response = RedirectResponse(
        f"/admin/account?link_error={urllib.parse.quote(msg)}", status_code=302
    )
    response.delete_cookie("link_identity_state")
    return response


async def handle_link_callback(request: Request, provider: IdentityProvider) -> RedirectResponse:
    """Handle an identity provider's callback for the self-service account
    linking flow, then redirect back to the Account page."""
    state_token = request.cookies.get("link_identity_state")
    if not state_token:
        return _link_error("Missing state cookie — possible CSRF")
    try:
        stored = _link_state_signer.loads(state_token, max_age=STATE_MAX_AGE)
    except BadSignature, SignatureExpired:
        return _link_error("Invalid or expired state — please try again")
    request_state = request.query_params.get("state")
    if not request_state or not secrets.compare_digest(request_state, stored["state"]):
        return _link_error("State mismatch — please try again")

    result = await provider.resolve_callback(request)
    if result is None:
        return _link_error("Authentication error — please try again")

    provider_slug, _, raw_id = result.username.partition(":")
    outcome = await access_control.link_identity(
        stored["user"], provider_slug, raw_id, result.display_name
    )
    if outcome != "ok":
        return _link_error(
            "That account is already linked to a different user"
            if outcome == "conflict"
            else "Your session is no longer valid — please log in again"
        )
    response = RedirectResponse("/admin/account", status_code=302)
    response.delete_cookie("link_identity_state")
    return response
