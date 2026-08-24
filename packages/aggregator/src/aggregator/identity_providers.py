"""
Identity providers for both auth surfaces: admin_auth.py's browser-session
flow and oauth.py's MCP OAuth 2.1 + PKCE flow. Each provider resolves a
callback request to a prefixed identity string ("github:octocat",
"steam:76561198012345678") -- callers never see provider-specific request
shapes, and neither provider knows about allowlists (see
access_control.resolve_login).
"""

import logging
import urllib.parse
from dataclasses import dataclass
from typing import Protocol

import httpx
from fastapi import Request
from fastapi.responses import RedirectResponse

from .config import GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, MCP_DOMAIN, STEAM_API_KEY

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

STEAM_OPENID_ENDPOINT = "https://steamcommunity.com/openid/login"
STEAM_CLAIMED_ID_PREFIX = "https://steamcommunity.com/openid/id/"


def _steam_http_client() -> httpx.AsyncClient:
    """Factory, not a module-level client -- tests monkeypatch this to
    inject a mocked transport without touching real network."""
    return httpx.AsyncClient(timeout=10.0)


class SteamProvider:
    slug = "steam"

    def is_configured(self) -> bool:
        return bool(STEAM_API_KEY)

    def login_redirect(self, state: str) -> RedirectResponse:
        return_to = f"https://{MCP_DOMAIN}/oauth/callback/steam?state={urllib.parse.quote(state)}"
        params = urllib.parse.urlencode(
            {
                "openid.ns": "http://specs.openid.net/auth/2.0",
                "openid.mode": "checkid_setup",
                "openid.return_to": return_to,
                "openid.realm": f"https://{MCP_DOMAIN}",
                "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
                "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
            }
        )
        return RedirectResponse(f"{STEAM_OPENID_ENDPOINT}?{params}", status_code=302)

    async def resolve_callback(self, request: Request) -> ProviderResult | None:
        params = dict(request.query_params)
        if params.get("openid.mode") != "id_res":
            logger.warning("Steam callback: unexpected openid.mode=%s", params.get("openid.mode"))
            return None
        expected_return_to = f"https://{MCP_DOMAIN}/oauth/callback/steam"
        return_to = params.get("openid.return_to", "")
        if not return_to.startswith(expected_return_to):
            logger.warning("Steam callback: unexpected openid.return_to")
            return None
        claimed_id = params.get("openid.claimed_id", "")
        if not claimed_id.startswith(STEAM_CLAIMED_ID_PREFIX):
            logger.warning("Steam callback: unexpected claimed_id shape: %s", claimed_id)
            return None
        steamid = claimed_id.removeprefix(STEAM_CLAIMED_ID_PREFIX)
        if not steamid.isdigit():
            logger.warning("Steam callback: claimed_id did not contain a numeric SteamID")
            return None

        # Validate that claimed_id, identity, and return_to were actually
        # signed by Steam. check_authentication proves Steam signed *some*
        # params, but not which ones -- an attacker could strip fields from a
        # genuine assertion (return_to matters here too: the return_to check
        # above is only tamper-proof if return_to is itself in this set).
        signed_fields = set(params.get("openid.signed", "").split(","))
        if not {"claimed_id", "identity", "return_to"} <= signed_fields:
            logger.warning("Steam callback: claimed_id/identity/return_to not in signed field set")
            return None

        # The security-critical step: Steam OpenID 2.0 has no client secret,
        # so a callback's authenticity is verified by POSTing the exact same
        # params back to Steam with openid.mode=check_authentication and
        # checking the response body contains "is_valid:true". Skipping
        # this lets an attacker forge a callback claiming any SteamID.
        verify_params = dict(params)
        verify_params["openid.mode"] = "check_authentication"
        try:
            async with _steam_http_client() as h:
                verify_resp = await h.post(STEAM_OPENID_ENDPOINT, data=verify_params)
                verify_resp.raise_for_status()
        except Exception as exc:
            logger.warning("Steam check_authentication error: %s", exc)
            return None
        if "is_valid:true" not in verify_resp.text:
            logger.warning("Steam callback: check_authentication rejected the response")
            return None

        display_name = steamid
        if STEAM_API_KEY:
            try:
                async with _steam_http_client() as h:
                    summary_resp = await h.get(
                        "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/",
                        params={"key": STEAM_API_KEY, "steamids": steamid},
                    )
                    summary_resp.raise_for_status()
                    players = summary_resp.json().get("response", {}).get("players", [])
                    if players:
                        display_name = players[0].get("personaname", steamid)
            except Exception as exc:
                logger.warning("Steam GetPlayerSummaries error: %s", exc)
                # Non-fatal: login still succeeds, just with the raw SteamID64.

        return ProviderResult(username=f"steam:{steamid}", display_name=display_name)


steam_provider = SteamProvider()

PROVIDERS: dict[str, IdentityProvider] = {
    "github": github_provider,
    "steam": steam_provider,
}


def configured_providers() -> list[IdentityProvider]:
    return [p for p in PROVIDERS.values() if p.is_configured()]


def get_provider(slug: str) -> IdentityProvider | None:
    return PROVIDERS.get(slug)
