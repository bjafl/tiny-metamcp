"""
Admin browser session auth via GitHub OAuth + itsdangerous signed cookies.

Separate from the MCP OAuth 2.1 flow (oauth.py) which serves AI clients.
Both flows use the same GitHub App credentials but different callback paths.
"""

import logging
import secrets
import urllib.parse

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import (
    ADMIN_TOKEN,
    GITHUB_ALLOWED_USERS,
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    MCP_DOMAIN,
    SESSION_SECRET,
)

logger = logging.getLogger(__name__)

_signer = URLSafeTimedSerializer(SESSION_SECRET, salt="admin-session")
_state_signer = URLSafeTimedSerializer(SESSION_SECRET, salt="admin-oauth-state")

SESSION_MAX_AGE = 7 * 86_400   # 7 days
STATE_MAX_AGE = 600              # 10 minutes


def get_session_user(request: Request) -> str | None:
    """Return authenticated GitHub username from session cookie, or None."""
    cookie = request.cookies.get("admin_session")
    if not cookie:
        return None
    try:
        username = _signer.loads(cookie, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if GITHUB_ALLOWED_USERS and username not in GITHUB_ALLOWED_USERS:
        return None
    return username


def require_admin(request: Request) -> str:
    """FastAPI dependency: returns username or redirects to login.

    For HTMX requests sends HX-Redirect (client-side page redirect) so the
    login page replaces the whole page, not a partial div.
    """
    user = get_session_user(request)
    if user is None:
        if request.headers.get("hx-request"):
            raise HTTPException(
                status_code=401,
                headers={"HX-Redirect": "/admin/login"},
            )
        raise HTTPException(status_code=307, headers={"Location": "/admin/login"})
    return user


def require_api_auth(request: Request) -> None:
    """FastAPI dependency for /api/* routes.

    Accepts an admin session cookie (browser tool-tester) or a Bearer
    ADMIN_TOKEN (programmatic access). Does not accept MCP OAuth tokens —
    those are only valid for /mcp and /messages.
    """
    if get_session_user(request):
        return
    auth = request.headers.get("authorization", "")
    if ADMIN_TOKEN and auth == f"Bearer {ADMIN_TOKEN}":
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


def login_redirect() -> RedirectResponse:
    """Start GitHub OAuth flow for admin browser login."""
    state = secrets.token_urlsafe(32)
    state_token = _state_signer.dumps(state)
    params = urllib.parse.urlencode({
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": f"https://{MCP_DOMAIN}/oauth/callback",
        "scope": "read:user",
        "state": state,
    })
    response = RedirectResponse(
        f"https://github.com/login/oauth/authorize?{params}", status_code=302
    )
    response.set_cookie(
        "admin_oauth_state", state_token,
        httponly=True, max_age=STATE_MAX_AGE, samesite="lax",
    )
    return response


async def handle_callback(request: Request) -> RedirectResponse:
    """Handle GitHub callback, set session cookie, redirect to /admin."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")
    error_description = request.query_params.get("error_description")

    def _login_error(msg: str) -> RedirectResponse:
        return RedirectResponse(
            f"/admin/login?error={urllib.parse.quote(msg)}", status_code=302
        )

    if error or not code or not state:
        return _login_error(error_description or error or "Missing code or state")

    # Verify state to prevent CSRF
    state_token = request.cookies.get("admin_oauth_state")
    if not state_token:
        return _login_error("Missing state cookie — possible CSRF")
    try:
        stored_state = _state_signer.loads(state_token, max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return _login_error("Invalid or expired state — please try again")
    if not secrets.compare_digest(state, stored_state):
        return _login_error("State mismatch — please try again")

    # Exchange code for GitHub identity
    try:
        async with httpx.AsyncClient(timeout=10.0) as h:
            token_resp = await h.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "client_secret": GITHUB_CLIENT_SECRET,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
            token_resp.raise_for_status()
            gh_token = token_resp.json().get("access_token")
            if not gh_token:
                raise ValueError(f"No access_token in response: {token_resp.json()}")
            user_resp = await h.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {gh_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            user_resp.raise_for_status()
            username: str = user_resp.json().get("login", "")
    except httpx.HTTPError as exc:
        logger.warning("Admin callback GitHub API error: %s", exc)
        return _login_error("GitHub API error — please try again")
    except Exception as exc:
        logger.warning("Admin callback error: %s", exc)
        return _login_error("Authentication error — please try again")

    if GITHUB_ALLOWED_USERS and username not in GITHUB_ALLOWED_USERS:
        logger.warning("Admin login denied: %s not in GITHUB_ALLOWED_USERS", username)
        return _login_error(f"User '{username}' is not authorized")

    logger.info("Admin login: %s", username)
    session_value = _signer.dumps(username)
    response = RedirectResponse("/admin", status_code=302)
    response.set_cookie(
        "admin_session", session_value,
        httponly=True, max_age=SESSION_MAX_AGE, secure=True, samesite="lax",
    )
    response.delete_cookie("admin_oauth_state")
    return response
