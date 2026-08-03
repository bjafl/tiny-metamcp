"""
OAuth 2.1 + PKCE authorization server backed by GitHub OAuth.

Flow for Claude Web UI connectors:
  1. /mcp returns 401 + WWW-Authenticate pointing at /.well-known/oauth-protected-resource
  2. Claude fetches discovery documents (/.well-known/oauth-authorization-server)
  3. Claude sends user to /authorize (PKCE + CIMD client_id)
  4. We redirect user to GitHub
  5. GitHub redirects to /oauth/callback
  6. We exchange GitHub code, validate user, issue auth code
  7. We redirect to client redirect_uri with auth code
  8. Claude POSTs to /token with code + code_verifier
  9. We verify PKCE, issue access_token + refresh_token
"""

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass

import aiosqlite
import httpx

from .config import (
    DB_PATH,
    GITHUB_ALLOWED_USERS,
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    MCP_DOMAIN,
)

logger = logging.getLogger(__name__)

ACCESS_TOKEN_TTL = 3600           # 1 hour
REFRESH_TOKEN_TTL = 30 * 86400    # 30 days
AUTH_CODE_TTL = 300               # 5 minutes
SESSION_TTL = 600                 # 10 minutes

# ── In-memory stores (short-lived, lost on restart — users re-auth) ──────────

@dataclass
class _Session:
    client_state: str    # original state from MCP client (passed back at end)
    client_id: str
    redirect_uri: str
    code_challenge: str
    expires_at: float

@dataclass
class _AuthCode:
    client_id: str
    redirect_uri: str
    code_challenge: str
    github_user: str
    expires_at: float

_sessions: dict[str, _Session] = {}   # github_state → _Session
_codes: dict[str, _AuthCode] = {}     # our_code → _AuthCode


def _gc() -> None:
    now = time.time()
    for d in (_sessions, _codes):
        for k in [k for k, v in d.items() if v.expires_at < now]:
            del d[k]


# ── PKCE ─────────────────────────────────────────────────────────────────────

def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(computed, code_challenge)


# ── Client validation (DCR registry + CIMD + hardcoded fallback) ─────────────

# Hardcoded fallback when CIMD fetch is unavailable
_KNOWN_CLIENTS: dict[str, list[str]] = {
    "https://claude.ai": ["https://claude.ai/api/mcp/auth_callback"],
    "https://www.claude.ai": ["https://claude.ai/api/mcp/auth_callback"],
}

# Dynamically registered clients (RFC7591); lost on restart, clients re-register
_registered_clients: dict[str, list[str]] = {}


def register_client(client_id: str, redirect_uris: list[str]) -> dict:
    """Register a dynamic client (RFC7591). Returns the registration record."""
    if not client_id:
        client_id = f"https://{MCP_DOMAIN}/clients/{secrets.token_urlsafe(16)}"
    _registered_clients[client_id] = redirect_uris
    logger.info("DCR: registered client %s with %d redirect URIs", client_id, len(redirect_uris))
    return {
        "client_id": client_id,
        "redirect_uris": redirect_uris,
        "client_id_issued_at": int(time.time()),
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


async def validate_client(client_id: str, redirect_uri: str) -> bool:
    """Validate client: DCR registry → CIMD fetch → hardcoded fallback."""
    if not client_id.startswith("https://"):
        return False
    if client_id in _registered_clients:
        return redirect_uri in _registered_clients[client_id]
    try:
        meta_url = client_id.rstrip("/") + "/.well-known/oauth-client"
        async with httpx.AsyncClient(timeout=5.0) as h:
            r = await h.get(meta_url, follow_redirects=True)
        if r.status_code == 200:
            return redirect_uri in r.json().get("redirect_uris", [])
    except Exception as exc:
        logger.debug("CIMD fetch failed for %s: %s", client_id, exc)
    return redirect_uri in _KNOWN_CLIENTS.get(client_id, [])


# ── Session lifecycle ─────────────────────────────────────────────────────────

def start_session(
    client_id: str, redirect_uri: str, code_challenge: str, client_state: str
) -> str:
    """Store pending OAuth session. Returns github_state to embed in GitHub redirect."""
    _gc()
    github_state = secrets.token_urlsafe(32)
    _sessions[github_state] = _Session(
        client_state=client_state,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        expires_at=time.time() + SESSION_TTL,
    )
    return github_state


async def finish_session(
    github_code: str, github_state: str
) -> tuple[str, str, str] | None:
    """
    Exchange GitHub code, validate user, issue auth code.
    Returns (auth_code, client_redirect_uri, client_state) or None on any failure.
    """
    session = _sessions.pop(github_state, None)
    if not session or session.expires_at < time.time():
        logger.warning("OAuth: unknown or expired state %s", github_state[:8])
        return None

    # Exchange GitHub authorization code for user identity
    try:
        async with httpx.AsyncClient(timeout=10.0) as h:
            r = await h.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "client_secret": GITHUB_CLIENT_SECRET,
                    "code": github_code,
                },
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            gh_token = r.json().get("access_token")
            if not gh_token:
                logger.warning("GitHub returned no access_token: %s", r.json())
                return None
            u = await h.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {gh_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            u.raise_for_status()
            github_user: str = u.json().get("login", "")
    except Exception as exc:
        logger.warning("GitHub exchange error: %s", exc)
        return None

    if GITHUB_ALLOWED_USERS and github_user not in GITHUB_ALLOWED_USERS:
        logger.warning("OAuth rejected: %s not in GITHUB_ALLOWED_USERS", github_user)
        return None

    _gc()
    code = secrets.token_urlsafe(32)
    _codes[code] = _AuthCode(
        client_id=session.client_id,
        redirect_uri=session.redirect_uri,
        code_challenge=session.code_challenge,
        github_user=github_user,
        expires_at=time.time() + AUTH_CODE_TTL,
    )
    logger.info("OAuth: auth code issued for %s (client=%s)", github_user, session.client_id)
    return code, session.redirect_uri, session.client_state


# ── Token exchange ────────────────────────────────────────────────────────────

async def exchange_code(
    code: str, code_verifier: str, client_id: str, redirect_uri: str
) -> tuple[str, str] | None:
    """Verify auth code + PKCE, issue (access_token, refresh_token)."""
    code_obj = _codes.pop(code, None)
    if not code_obj or code_obj.expires_at < time.time():
        return None
    if code_obj.client_id != client_id or code_obj.redirect_uri != redirect_uri:
        return None
    if not verify_pkce(code_verifier, code_obj.code_challenge):
        return None

    access = secrets.token_urlsafe(48)
    refresh = secrets.token_urlsafe(48)
    await _store_token(access, "access", code_obj.github_user, client_id, ACCESS_TOKEN_TTL)
    await _store_token(refresh, "refresh", code_obj.github_user, client_id, REFRESH_TOKEN_TTL)
    logger.info("OAuth: tokens issued for %s", code_obj.github_user)
    return access, refresh


async def rotate_refresh(refresh_token: str) -> tuple[str, str] | None:
    """Validate refresh token, rotate to new (access_token, refresh_token)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT github_user, client_id FROM oauth_tokens "
            "WHERE token=? AND token_type='refresh' AND expires_at>?",
            (refresh_token, time.time()),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        github_user, client_id = row
        await db.execute("DELETE FROM oauth_tokens WHERE token=?", (refresh_token,))
        await db.commit()

    access = secrets.token_urlsafe(48)
    refresh = secrets.token_urlsafe(48)
    await _store_token(access, "access", github_user, client_id, ACCESS_TOKEN_TTL)
    await _store_token(refresh, "refresh", github_user, client_id, REFRESH_TOKEN_TTL)
    logger.info("OAuth: tokens rotated for %s", github_user)
    return access, refresh


async def validate_bearer(token: str) -> str | None:
    """Return github_user if token is a valid OAuth access token, else None."""
    async with aiosqlite.connect(DB_PATH) as db, db.execute(
        "SELECT github_user FROM oauth_tokens "
        "WHERE token=? AND token_type='access' AND expires_at>?",
        (token, time.time()),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def cleanup_expired() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM oauth_tokens WHERE expires_at<?", (time.time(),))
        await db.commit()


async def _store_token(
    token: str, token_type: str, github_user: str, client_id: str, ttl: int
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO oauth_tokens "
            "(token, token_type, github_user, client_id, expires_at) VALUES (?,?,?,?,?)",
            (token, token_type, github_user, client_id, time.time() + ttl),
        )
        await db.commit()


# ── Discovery metadata ────────────────────────────────────────────────────────

def protected_resource_metadata() -> dict:
    base = f"https://{MCP_DOMAIN}"
    return {
        "resource": base,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{base}/admin",
    }


def authorization_server_metadata() -> dict:
    base = f"https://{MCP_DOMAIN}"
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    }


def www_authenticate_header() -> str:
    return (
        f'Bearer realm="MCP Aggregator",'
        f' resource_metadata="https://{MCP_DOMAIN}/.well-known/oauth-protected-resource"'
    )
