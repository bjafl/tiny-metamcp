"""
Tests that admin_auth.get_session_user() correctly rejects a garbage or
expired session cookie by returning None, rather than raising, and that
require_api_auth() enforces personal-token auth as expected.
"""

import pytest
from fastapi import HTTPException, Request

from aggregator import access_control
from aggregator.admin_auth import get_session_user, require_api_auth


def _request_with_cookie(cookie_value: str) -> Request:
    scope = {
        "type": "http",
        "headers": [(b"cookie", f"admin_session={cookie_value}".encode())],
    }
    return Request(scope)


def test_get_session_user_returns_none_for_garbage_cookie():
    assert get_session_user(_request_with_cookie("not-a-real-signed-value")) is None


def test_get_session_user_returns_none_when_no_cookie():
    assert get_session_user(Request({"type": "http", "headers": []})) is None


def _request_with_headers(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


async def test_require_api_auth_accepts_valid_personal_token():
    token = await access_control.generate_personal_token("auth-test-user")
    username = await require_api_auth(_request_with_headers({"authorization": f"Bearer {token}"}))
    assert username == "auth-test-user"


async def test_require_api_auth_rejects_unknown_bearer_token():
    with pytest.raises(HTTPException) as exc_info:
        await require_api_auth(_request_with_headers({"authorization": "Bearer not-a-real-token"}))
    assert exc_info.value.status_code == 401


async def test_require_api_auth_rejects_missing_auth():
    with pytest.raises(HTTPException) as exc_info:
        await require_api_auth(Request({"type": "http", "headers": []}))
    assert exc_info.value.status_code == 401
