"""
Tests for the /api/me and /api/me/token routes on main.py's app.

Unlike everything under api_router (routers.py), these two routes are
defined directly on main.app and authenticate solely via
admin_auth.get_session_user (a signed admin_session cookie) -- they do not
accept a personal-token Bearer header. A session cookie is faked here via
admin_auth's own itsdangerous signer, since driving a full GitHub OAuth
handshake isn't practical in a unit test; this still exercises the same
get_session_user()/generate_personal_token() code paths a real logged-in
webui session would hit.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from aggregator import access_control, admin_auth
from aggregator.main import app

ADMIN = "test-admin"  # set as ADMIN_USERS by conftest.py
USER = "me-routes-user"


def _session_cookie(username: str, display_name: str | None = None) -> str:
    return admin_auth._signer.dumps({"username": username, "display_name": display_name or username})


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_me_requires_session_cookie(client):
    resp = await client.get("/api/me")
    assert resp.status_code == 401


async def test_me_returns_username_and_admin_flag(client):
    client.cookies.set("admin_session", _session_cookie(USER))
    resp = await client.get("/api/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == USER
    assert body["is_admin"] is False
    assert body["display_name"] == USER


async def test_me_reports_admin_true_for_admin_user(client):
    client.cookies.set("admin_session", _session_cookie(ADMIN))
    resp = await client.get("/api/me")
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is True


async def test_me_rejects_garbage_cookie(client):
    client.cookies.set("admin_session", "not-a-real-signed-value")
    resp = await client.get("/api/me")
    assert resp.status_code == 401


async def test_me_token_requires_session_cookie(client):
    resp = await client.post("/api/me/token")
    assert resp.status_code == 401


async def test_me_token_generates_working_personal_token(client):
    client.cookies.set("admin_session", _session_cookie(USER))
    resp = await client.post("/api/me/token")
    assert resp.status_code == 200
    token = resp.json()["token"]
    assert await access_control.validate_personal_token(token) == USER


async def test_me_token_rejects_bearer_auth():
    """/api/me/token is session-cookie only -- a bearer token must not
    satisfy it, unlike the api_router routes which accept either."""
    token = await access_control.generate_personal_token(USER)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/api/me/token", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
