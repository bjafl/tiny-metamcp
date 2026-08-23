"""
Identity providers for both auth surfaces: admin_auth.py's browser-session
flow and oauth.py's MCP OAuth 2.1 + PKCE flow. Each provider resolves a
callback request to a prefixed identity string ("github:octocat",
"steam:76561198012345678") -- callers never see provider-specific request
shapes, and neither provider knows about allowlists (see
access_control.is_allowed).
"""

import logging
import urllib.parse
from dataclasses import dataclass
from typing import Protocol

import httpx
from fastapi import Request
from fastapi.responses import RedirectResponse

from .config import GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, MCP_DOMAIN

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderResult:
    username: str  # prefixed identity, e.g. "github:octocat"
    display_name: str  # persona name (Steam) or login (GitHub, same as username's suffix)


class IdentityProvider(Protocol):
    slug: str

    def is_configured(self) -> bool: ...
    def login_redirect(self, state: str) -> RedirectResponse: ...
    async def resolve_callback(self, request: Request) -> ProviderResult | None: ...


def _github_http_client() -> httpx.AsyncClient:
    """Factory, not a module-level client -- tests monkeypatch this to
    inject a mocked transport without touching real network."""
    return httpx.AsyncClient(timeout=10.0)


class GitHubProvider:
    slug = "github"

    def is_configured(self) -> bool:
        return bool(GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET)

    def login_redirect(self, state: str) -> RedirectResponse:
        params = urllib.parse.urlencode(
            {
                "client_id": GITHUB_CLIENT_ID,
                "redirect_uri": f"https://{MCP_DOMAIN}/oauth/callback",
                "scope": "read:user",
                "state": state,
            }
        )
        return RedirectResponse(
            f"https://github.com/login/oauth/authorize?{params}", status_code=302
        )

    async def resolve_callback(self, request: Request) -> ProviderResult | None:
        code = request.query_params.get("code")
        error = request.query_params.get("error")
        if error or not code:
            logger.warning("GitHub callback error: %s", error or "missing code")
            return None
        try:
            async with _github_http_client() as h:
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
                    logger.warning("GitHub returned no access_token: %s", token_resp.json())
                    return None
                user_resp = await h.get(
                    "https://api.github.com/user",
                    headers={
                        "Authorization": f"Bearer {gh_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                user_resp.raise_for_status()
                login: str = user_resp.json().get("login", "")
        except Exception as exc:
            logger.warning("GitHub exchange error: %s", exc)
            return None
        if not login:
            return None
        return ProviderResult(username=f"github:{login}", display_name=login)


github_provider = GitHubProvider()
