"""
Tests for api/oauth_router.py's provider-aware /authorize branching and
per-provider /oauth/callback* routes. Uses a minimal FastAPI app (just
oauth_router) over httpx's ASGI transport.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aggregator import access_control, identity_providers, oauth
from aggregator.api.oauth_router import router as oauth_router
from aggregator.identity_providers import ProviderResult


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(oauth_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        yield c


def _authorize_params(**overrides) -> dict:
    params = {
        "response_type": "code",
        "client_id": "https://claude.ai",
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "state": "client-state",
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
    }
    params.update(overrides)
    return params


async def test_authorize_redirects_directly_when_one_provider_configured(client, monkeypatch):
    # oauth.validate_client() falls back to a live CIMD fetch to the
    # client's own domain before consulting the hardcoded _KNOWN_CLIENTS
    # dict -- mock it directly so this test never makes a real network call.
    monkeypatch.setattr(oauth, "validate_client", AsyncMock(return_value=True))
    monkeypatch.setattr(
        identity_providers, "PROVIDERS", {"github": identity_providers.github_provider}
    )
    monkeypatch.setattr(identity_providers.github_provider, "is_configured", lambda: True)

    resp = await client.get("/authorize", params=_authorize_params())
    assert resp.status_code == 302
    assert "github.com/login/oauth/authorize" in resp.headers["location"]


async def test_authorize_shows_chooser_when_multiple_providers_configured(client, monkeypatch):
    monkeypatch.setattr(oauth, "validate_client", AsyncMock(return_value=True))
    monkeypatch.setattr(
        identity_providers,
        "PROVIDERS",
        {"github": identity_providers.github_provider, "steam": identity_providers.steam_provider},
    )
    monkeypatch.setattr(identity_providers.github_provider, "is_configured", lambda: True)
    monkeypatch.setattr(identity_providers.steam_provider, "is_configured", lambda: True)

    resp = await client.get("/authorize", params=_authorize_params())
    assert resp.status_code == 200
    assert "github" in resp.text.lower()
    assert "steam" in resp.text.lower()
    assert "/authorize/continue?provider=github" in resp.text
    assert "/authorize/continue?provider=steam" in resp.text


async def test_authorize_returns_500_when_no_provider_configured(client, monkeypatch):
    monkeypatch.setattr(oauth, "validate_client", AsyncMock(return_value=True))
    monkeypatch.setattr(identity_providers, "PROVIDERS", {})

    resp = await client.get("/authorize", params=_authorize_params())
    assert resp.status_code == 500


async def test_authorize_continue_redirects_to_named_provider(client, monkeypatch):
    monkeypatch.setattr(identity_providers.steam_provider, "is_configured", lambda: True)
    resp = await client.get("/authorize/continue", params={"provider": "steam", "state": "abc"})
    assert resp.status_code == 302
    assert "steamcommunity.com/openid/login" in resp.headers["location"]


async def test_authorize_continue_rejects_unknown_provider(client):
    resp = await client.get("/authorize/continue", params={"provider": "discord", "state": "abc"})
    assert resp.status_code == 400


async def test_oauth_callback_github_admin_flow_delegates_to_admin_auth(client, monkeypatch):
    called = {}

    async def fake_handle_callback(request, provider):
        called["provider"] = provider.slug
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/admin", status_code=302)

    monkeypatch.setattr(
        "aggregator.api.oauth_router.admin_auth.handle_callback", fake_handle_callback
    )

    resp = await client.get(
        "/oauth/callback",
        params={"code": "abc", "state": "xyz"},
        cookies={"admin_oauth_state": "present"},
    )
    assert resp.status_code == 302
    assert called["provider"] == "github"


async def test_oauth_callback_github_mcp_flow_issues_redirect_with_code(client, monkeypatch):
    monkeypatch.setattr(
        identity_providers.github_provider,
        "resolve_callback",
        AsyncMock(return_value=ProviderResult(username="github:octocat", display_name="octocat")),
    )
    monkeypatch.setattr(access_control, "resolve_login", AsyncMock(return_value="user:1"))
    monkeypatch.setattr(
        oauth,
        "finish_session",
        AsyncMock(return_value=("auth-code", "https://client.example/cb", "client-state")),
    )

    resp = await client.get("/oauth/callback", params={"code": "abc", "state": "xyz"})
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://client.example/cb?")
    assert "code=auth-code" in resp.headers["location"]


async def test_oauth_callback_steam_mcp_flow_issues_redirect_with_code(client, monkeypatch):
    monkeypatch.setattr(
        identity_providers.steam_provider,
        "resolve_callback",
        AsyncMock(return_value=ProviderResult(username="steam:765", display_name="Gamer")),
    )
    monkeypatch.setattr(access_control, "resolve_login", AsyncMock(return_value="user:2"))
    monkeypatch.setattr(
        oauth,
        "finish_session",
        AsyncMock(return_value=("auth-code", "https://client.example/cb", "client-state")),
    )

    resp = await client.get("/oauth/callback/steam", params={"state": "xyz"})
    assert resp.status_code == 302
    assert "code=auth-code" in resp.headers["location"]


async def test_oauth_callback_returns_400_when_state_missing(client):
    resp = await client.get("/oauth/callback", params={"error": "access_denied"})
    assert resp.status_code == 400


async def test_oauth_callback_returns_403_when_provider_resolve_fails(client, monkeypatch):
    monkeypatch.setattr(
        identity_providers.github_provider, "resolve_callback", AsyncMock(return_value=None)
    )
    resp = await client.get("/oauth/callback", params={"error": "access_denied", "state": "xyz"})
    assert resp.status_code == 403


async def test_oauth_callback_returns_403_when_resolve_login_denies(client, monkeypatch):
    monkeypatch.setattr(
        identity_providers.github_provider,
        "resolve_callback",
        AsyncMock(return_value=ProviderResult(username="github:not-allowed", display_name="X")),
    )
    monkeypatch.setattr(access_control, "resolve_login", AsyncMock(return_value=None))

    resp = await client.get("/oauth/callback", params={"code": "abc", "state": "xyz"})
    assert resp.status_code == 403


async def test_oauth_callback_link_flow_delegates_to_admin_auth(client, monkeypatch):
    called = {}

    async def fake_handle_link_callback(request, provider):
        called["provider"] = provider.slug
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/admin/account", status_code=302)

    monkeypatch.setattr(
        "aggregator.api.oauth_router.admin_auth.handle_link_callback", fake_handle_link_callback
    )

    resp = await client.get(
        "/oauth/callback",
        params={"code": "abc", "state": "xyz"},
        cookies={"link_identity_state": "present"},
    )
    assert resp.status_code == 302
    assert called["provider"] == "github"
