"""
Tests for identity_providers.py -- the IdentityProvider abstraction shared
by admin_auth.py's browser-session flow and oauth.py's MCP PKCE flow.

There is no local GitHub/Steam to talk to, so these tests mock the
outbound httpx calls -- the one place in this project's test suite where
mocking the transport is the right tool rather than a shortcut, since the
whole point is verifying behavior against a *third-party* service's HTTP
contract, not our own code's transport handling.
"""

import urllib.parse

import httpx
from fastapi import Request

from aggregator import identity_providers


def _request_with_query(query_string: str) -> Request:
    return Request(
        {
            "type": "http",
            "query_string": query_string.encode(),
            "headers": [],
        }
    )


def test_github_provider_is_configured_true_when_both_set(monkeypatch):
    monkeypatch.setattr(identity_providers, "GITHUB_CLIENT_ID", "id")
    monkeypatch.setattr(identity_providers, "GITHUB_CLIENT_SECRET", "secret")
    assert identity_providers.github_provider.is_configured()


def test_github_provider_is_configured_false_when_either_missing(monkeypatch):
    monkeypatch.setattr(identity_providers, "GITHUB_CLIENT_ID", "")
    monkeypatch.setattr(identity_providers, "GITHUB_CLIENT_SECRET", "secret")
    assert not identity_providers.github_provider.is_configured()


def test_github_provider_login_redirect_targets_github_with_state():
    response = identity_providers.github_provider.login_redirect("my-state-value")
    assert response.status_code == 302
    assert "github.com/login/oauth/authorize" in response.headers["location"]
    assert "state=my-state-value" in response.headers["location"]


async def test_github_provider_resolve_callback_returns_prefixed_result(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "gh-token"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octocat"})
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(
        identity_providers,
        "_github_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0),
    )

    result = await identity_providers.github_provider.resolve_callback(
        _request_with_query("code=abc123&state=xyz")
    )
    assert result == identity_providers.ProviderResult(
        username="github:octocat", display_name="octocat"
    )


async def test_github_provider_resolve_callback_returns_none_on_error_param():
    result = await identity_providers.github_provider.resolve_callback(
        _request_with_query("error=access_denied")
    )
    assert result is None


async def test_github_provider_resolve_callback_returns_none_on_missing_code():
    result = await identity_providers.github_provider.resolve_callback(_request_with_query(""))
    assert result is None


async def test_github_provider_resolve_callback_returns_none_when_no_access_token(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        identity_providers,
        "_github_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0),
    )
    result = await identity_providers.github_provider.resolve_callback(
        _request_with_query("code=abc123&state=xyz")
    )
    assert result is None


def test_steam_provider_is_configured_true_when_api_key_set(monkeypatch):
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "some-key")
    assert identity_providers.steam_provider.is_configured()


def test_steam_provider_is_configured_false_when_api_key_unset(monkeypatch):
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "")
    assert not identity_providers.steam_provider.is_configured()


def test_steam_provider_login_redirect_targets_steam_with_state_in_return_to():
    response = identity_providers.steam_provider.login_redirect("my-state-value")
    assert response.status_code == 302
    location = response.headers["location"]
    assert "steamcommunity.com/openid/login" in location
    assert "openid.mode=checkid_setup" in location
    # the state travels inside the (urlencoded) openid.return_to URL, not
    # as its own top-level query param -- decode return_to and check there
    parsed = urllib.parse.urlparse(location)
    qs = urllib.parse.parse_qs(parsed.query)
    return_to = qs["openid.return_to"][0]
    assert "state=my-state-value" in urllib.parse.urlparse(return_to).query


async def test_steam_provider_resolve_callback_accepts_valid_response(monkeypatch):
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "steamcommunity.com":
            assert b"openid.mode=check_authentication" in request.content
            return httpx.Response(200, text="ns:http://specs.openid.net/auth/2.0\nis_valid:true\n")
        if request.url.host == "api.steampowered.com":
            return httpx.Response(
                200,
                json={"response": {"players": [{"personaname": "CoolGamer99"}]}},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(
        identity_providers,
        "_steam_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0),
    )

    result = await identity_providers.steam_provider.resolve_callback(
        _request_with_query(
            "openid.mode=id_res&openid.claimed_id="
            "https%3A%2F%2Fsteamcommunity.com%2Fopenid%2Fid%2F76561198012345678"
            "&openid.sig=abc"
        )
    )
    assert result == identity_providers.ProviderResult(
        username="steam:76561198012345678", display_name="CoolGamer99"
    )


async def test_steam_provider_resolve_callback_rejects_forged_response(monkeypatch):
    """The security-critical case: a callback with valid-looking openid.*
    params but a check_authentication response of is_valid:false must be
    rejected -- this is what stops an attacker from forging a callback
    claiming an arbitrary SteamID."""
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ns:http://specs.openid.net/auth/2.0\nis_valid:false\n")

    monkeypatch.setattr(
        identity_providers,
        "_steam_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0),
    )

    result = await identity_providers.steam_provider.resolve_callback(
        _request_with_query(
            "openid.mode=id_res&openid.claimed_id="
            "https%3A%2F%2Fsteamcommunity.com%2Fopenid%2Fid%2F76561198012345678"
            "&openid.sig=forged"
        )
    )
    assert result is None


async def test_steam_provider_resolve_callback_rejects_wrong_mode():
    result = await identity_providers.steam_provider.resolve_callback(
        _request_with_query("openid.mode=cancel")
    )
    assert result is None


async def test_steam_provider_resolve_callback_falls_back_to_steamid_without_api_key(monkeypatch):
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "steamcommunity.com"
        return httpx.Response(200, text="ns:http://specs.openid.net/auth/2.0\nis_valid:true\n")

    monkeypatch.setattr(
        identity_providers,
        "_steam_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0),
    )

    result = await identity_providers.steam_provider.resolve_callback(
        _request_with_query(
            "openid.mode=id_res&openid.claimed_id="
            "https%3A%2F%2Fsteamcommunity.com%2Fopenid%2Fid%2F76561198012345678"
            "&openid.sig=abc"
        )
    )
    assert result == identity_providers.ProviderResult(
        username="steam:76561198012345678", display_name="76561198012345678"
    )


def test_configured_providers_reflects_which_are_set(monkeypatch):
    monkeypatch.setattr(identity_providers, "GITHUB_CLIENT_ID", "id")
    monkeypatch.setattr(identity_providers, "GITHUB_CLIENT_SECRET", "secret")
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "")
    assert identity_providers.configured_providers() == [identity_providers.github_provider]


def test_get_provider_returns_matching_provider_or_none():
    assert identity_providers.get_provider("github") is identity_providers.github_provider
    assert identity_providers.get_provider("steam") is identity_providers.steam_provider
    assert identity_providers.get_provider("discord") is None
