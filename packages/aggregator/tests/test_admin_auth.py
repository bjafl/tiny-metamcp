"""
Tests that admin_auth.get_session_user()/get_session_display_name()
correctly decode (or reject) a session cookie, that require_api_auth()
enforces personal-token auth as expected, and that login_redirect()/
handle_callback() correctly delegate to an IdentityProvider.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from aggregator import access_control, admin_auth
from aggregator.admin_auth import get_session_display_name, get_session_user, require_api_auth
from aggregator.identity_providers import ProviderResult


def _request_with_cookie(cookie_value: str) -> Request:
    scope = {
        "type": "http",
        "headers": [(b"cookie", f"admin_session={cookie_value}".encode())],
    }
    return Request(scope)


def _session_cookie(username: str, display_name: str) -> str:
    return admin_auth._signer.dumps({"username": username, "display_name": display_name})


def test_get_session_user_returns_none_for_garbage_cookie():
    assert get_session_user(_request_with_cookie("not-a-real-signed-value")) is None


def test_get_session_user_returns_none_when_no_cookie():
    assert get_session_user(Request({"type": "http", "headers": []})) is None


def test_get_session_user_returns_none_for_legacy_plain_string_payload():
    """Sessions issued before the Steam-login change signed a plain
    username string, not a {"username", "display_name"} dict. These must
    be treated as invalid (forcing re-login), not crash."""
    legacy_cookie = admin_auth._signer.dumps("octocat")
    assert get_session_user(_request_with_cookie(legacy_cookie)) is None


def test_get_session_user_returns_username_for_valid_cookie(monkeypatch):
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", set())
    cookie = _session_cookie("github:octocat", "octocat")
    assert get_session_user(_request_with_cookie(cookie)) == "github:octocat"


def test_get_session_user_returns_none_when_not_allowed(monkeypatch):
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", {"someone-else"})
    cookie = _session_cookie("github:octocat", "octocat")
    assert get_session_user(_request_with_cookie(cookie)) is None


def test_get_session_display_name_returns_display_name(monkeypatch):
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", set())
    cookie = _session_cookie("steam:76561198012345678", "CoolGamer99")
    assert get_session_display_name(_request_with_cookie(cookie)) == "CoolGamer99"


def test_get_session_display_name_returns_none_for_garbage_cookie():
    assert get_session_display_name(_request_with_cookie("garbage")) is None


def _request_with_headers(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


async def test_require_api_auth_accepts_valid_personal_token():
    token = await access_control.generate_personal_token("github:auth-test-user")
    username = await require_api_auth(_request_with_headers({"authorization": f"Bearer {token}"}))
    assert username == "github:auth-test-user"


async def test_require_api_auth_rejects_unknown_bearer_token():
    with pytest.raises(HTTPException) as exc_info:
        await require_api_auth(_request_with_headers({"authorization": "Bearer not-a-real-token"}))
    assert exc_info.value.status_code == 401


async def test_require_api_auth_rejects_missing_auth():
    with pytest.raises(HTTPException) as exc_info:
        await require_api_auth(Request({"type": "http", "headers": []}))
    assert exc_info.value.status_code == 401


class _FakeProvider:
    slug = "fake"

    def is_configured(self) -> bool:
        return True

    def login_redirect(self, state: str) -> RedirectResponse:
        return RedirectResponse(
            f"https://fake-provider.example/authorize?state={state}", status_code=302
        )

    async def resolve_callback(self, request: Request) -> ProviderResult | None:
        raise NotImplementedError  # overridden per-test via monkeypatch/mock


def test_login_redirect_sets_state_cookie_and_delegates_to_provider():
    response = admin_auth.login_redirect(_FakeProvider())
    assert response.status_code == 302
    assert "fake-provider.example" in response.headers["location"]
    assert "admin_oauth_state=" in response.headers.get("set-cookie", "")


async def test_handle_callback_sets_session_cookie_on_success(monkeypatch):
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", set())
    provider = _FakeProvider()
    provider.resolve_callback = AsyncMock(
        return_value=ProviderResult(username="github:octocat", display_name="octocat")
    )

    # Simulate the state round-trip: login_redirect signs+cookies a state,
    # the "callback" request carries that same state back as a query param.
    login_response = admin_auth.login_redirect(provider)
    state_cookie = login_response.headers["set-cookie"]
    # extract admin_oauth_state=<value> up to the next ';'
    cookie_value = state_cookie.split("admin_oauth_state=")[1].split(";")[0]
    raw_state = admin_auth._state_signer.loads(cookie_value, max_age=admin_auth.STATE_MAX_AGE)

    request = Request(
        {
            "type": "http",
            "query_string": f"state={raw_state}".encode(),
            "headers": [(b"cookie", f"admin_oauth_state={cookie_value}".encode())],
        }
    )
    response = await admin_auth.handle_callback(request, provider)
    assert response.status_code == 302
    assert response.headers["location"] == "/admin"
    assert "admin_session=" in response.headers.get("set-cookie", "")


async def test_handle_callback_rejects_state_mismatch():
    provider = _FakeProvider()
    request = Request(
        {
            "type": "http",
            "query_string": b"state=wrong-state",
            "headers": [
                (
                    b"cookie",
                    b"admin_oauth_state=" + admin_auth._state_signer.dumps("real-state").encode(),
                )
            ],
        }
    )
    response = await admin_auth.handle_callback(request, provider)
    assert response.status_code == 302
    assert "error=" in response.headers["location"]


async def test_handle_callback_rejects_when_provider_returns_none():
    provider = _FakeProvider()
    provider.resolve_callback = AsyncMock(return_value=None)
    state_token = admin_auth._state_signer.dumps("real-state")
    request = Request(
        {
            "type": "http",
            "query_string": b"state=real-state",
            "headers": [(b"cookie", f"admin_oauth_state={state_token}".encode())],
        }
    )
    response = await admin_auth.handle_callback(request, provider)
    assert response.status_code == 302
    assert "error=" in response.headers["location"]
