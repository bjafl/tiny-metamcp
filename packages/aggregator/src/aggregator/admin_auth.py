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

SESSION_MAX_AGE = 7 * 86_400  # 7 days
STATE_MAX_AGE = 600  # 10 minutes


def _load_session_payload(request: Request) -> dict | None:
    cookie = request.cookies.get("admin_session")
    if not cookie:
        return None
    try:
        payload = _signer.loads(cookie, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    # Sessions issued before multi-provider support signed a plain string,
    # not a {"username", "display_name"} dict -- treat as invalid rather
    # than crash, forcing a one-time re-login.
    if not isinstance(payload, dict):
        return None
    return payload


def get_session_user(request: Request) -> str | None:
    """Return the authenticated (prefixed) username from the session
    cookie, or None."""
    payload = _load_session_payload(request)
    if payload is None:
        return None
    username = payload.get("username")
    if not username or not access_control.is_allowed(username):
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
    user = get_session_user(request)
    if user:
        return user
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        username = await access_control.validate_personal_token(auth[7:])
        if username:
            return username
    raise HTTPException(status_code=401, detail="Unauthorized")


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
    except (BadSignature, SignatureExpired):
        return _login_error("Invalid or expired state — please try again")
    request_state = request.query_params.get("state")
    if not request_state or not secrets.compare_digest(request_state, stored_state):
        return _login_error("State mismatch — please try again")

    result = await provider.resolve_callback(request)
    if result is None:
        return _login_error("Authentication error — please try again")

    if not access_control.is_allowed(result.username):
        logger.warning("Admin login denied: %s not allowed", result.username)
        return _login_error(f"User '{result.username}' is not authorized")

    logger.info("Admin login: %s", result.username)
    session_value = _signer.dumps({"username": result.username, "display_name": result.display_name})
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
