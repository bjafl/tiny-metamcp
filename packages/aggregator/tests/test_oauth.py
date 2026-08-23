"""
Tests for oauth.py's provider-agnostic session/token issuing. The actual
identity resolution (GitHub/Steam HTTP exchange) lives in
identity_providers.py and is tested there -- these tests cover
start_session/finish_session/exchange_code/rotate_refresh/validate_bearer
purely in terms of an already-resolved username.
"""

from aggregator import oauth


async def test_finish_session_issues_auth_code_for_allowed_user(monkeypatch):
    monkeypatch.setattr("aggregator.oauth.access_control.is_allowed", lambda u: True)
    state = oauth.start_session(
        "client-1", "https://client.example/cb", "challenge", "client-state"
    )
    result = await oauth.finish_session(state, "github:octocat")
    assert result is not None
    code, redirect_uri, client_state = result
    assert redirect_uri == "https://client.example/cb"
    assert client_state == "client-state"


async def test_finish_session_rejects_disallowed_user(monkeypatch):
    monkeypatch.setattr("aggregator.oauth.access_control.is_allowed", lambda u: False)
    state = oauth.start_session(
        "client-1", "https://client.example/cb", "challenge", "client-state"
    )
    result = await oauth.finish_session(state, "github:not-allowed")
    assert result is None


async def test_finish_session_rejects_unknown_state():
    result = await oauth.finish_session("not-a-real-state", "github:octocat")
    assert result is None


def _pkce_pair(verifier: str) -> tuple[str, str]:
    import base64
    import hashlib

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


async def test_exchange_code_and_validate_bearer_round_trip(monkeypatch):
    monkeypatch.setattr("aggregator.oauth.access_control.is_allowed", lambda u: True)
    verifier, challenge = _pkce_pair("a-real-code-verifier-at-least-43-characters-long")
    state = oauth.start_session("client-1", "https://client.example/cb", challenge, "cs")
    finish_result = await oauth.finish_session(state, "github:octocat")
    code, _, _ = finish_result

    result = await oauth.exchange_code(code, verifier, "client-1", "https://client.example/cb")
    assert result is not None
    access_token, refresh_token = result
    assert await oauth.validate_bearer(access_token) == "github:octocat"


async def test_exchange_code_rejects_pkce_mismatch(monkeypatch):
    monkeypatch.setattr("aggregator.oauth.access_control.is_allowed", lambda u: True)
    _, challenge = _pkce_pair("the-real-verifier-that-was-registered-at-start-session")
    state = oauth.start_session("client-1", "https://client.example/cb", challenge, "cs")
    finish_result = await oauth.finish_session(state, "github:octocat")
    code, _, _ = finish_result

    result = await oauth.exchange_code(
        code, "wrong-verifier", "client-1", "https://client.example/cb"
    )
    assert result is None


async def test_validate_bearer_returns_none_for_unknown_token():
    assert await oauth.validate_bearer("not-a-real-token") is None
