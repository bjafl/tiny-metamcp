"""
Regression test for the admin_auth module actually being importable and
get_session_user() correctly rejecting a garbage/expired session cookie
without raising -- it previously used Python 2 `except X, Y:` syntax, a
SyntaxError under Python 3 that made the whole module fail to import.
"""

from fastapi import Request

from aggregator.admin_auth import get_session_user


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
