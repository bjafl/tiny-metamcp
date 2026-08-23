"""
Tests for identity_providers.py -- the IdentityProvider abstraction shared
by admin_auth.py's browser-session flow and oauth.py's MCP PKCE flow.

There is no local GitHub/Steam to talk to, so these tests mock the
outbound httpx calls -- the one place in this project's test suite where
mocking the transport is the right tool rather than a shortcut, since the
whole point is verifying behavior against a *third-party* service's HTTP
contract, not our own code's transport handling.
"""

import httpx
import pytest
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
