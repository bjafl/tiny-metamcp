"""
Tests for the /api/me, /api/me/token, /admin/link/{provider}, and
/api/me/identities/{id} routes on main.py's app.

Unlike everything under api_router (routers.py), these routes are defined
directly on main.app. /api/me and /api/me/token authenticate solely via
admin_auth.get_session_user (a signed admin_session cookie) -- they do not
accept a personal-token Bearer header. A session cookie is faked here via
admin_auth's own itsdangerous signer, since driving a full GitHub OAuth
handshake isn't practical in a unit test; this still exercises the same
get_session_user()/generate_personal_token() code paths a real logged-in
webui session would hit.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from aggregator import admin_auth
from aggregator.main import app


def _session_cookie(username: str, display_name: str | None = None) -> str:
    return admin_auth._signer.dumps(
        {"username": username, "display_name": display_name or username}
    )


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _make_user(raw_id: str, *, is_admin: bool = False) -> str:
    from aggregator import access_control, database

    canonical = await access_control.resolve_login("github", raw_id, raw_id)
    if is_admin:
        await database.update_user_flags(int(canonical.removeprefix("user:")), is_admin=True)
    return canonical


async def test_me_requires_session_cookie(client):
    resp = await client.get("/api/me")
    assert resp.status_code == 401


async def test_me_returns_username_and_admin_flag(client):
    user = await _make_user("me-routes-user")
    client.cookies.set("admin_session", _session_cookie(user))
    resp = await client.get("/api/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == user
    assert body["is_admin"] is False
    assert body["display_name"] == user
    assert len(body["identities"]) == 1
    identity = body["identities"][0]
    assert identity["provider"] == "github"
    assert identity["raw_id"] == "me-routes-user"
    assert identity["display_name"] == "me-routes-user"


async def test_me_reports_admin_true_for_admin_user(client):
    admin = await _make_user("me-routes-admin", is_admin=True)
    client.cookies.set("admin_session", _session_cookie(admin))
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
    from aggregator import access_control

    user = await _make_user("me-token-user")
    client.cookies.set("admin_session", _session_cookie(user))
    resp = await client.post("/api/me/token")
    assert resp.status_code == 200
    token = resp.json()["token"]
    assert await access_control.validate_personal_token(token) == user


async def test_me_token_rejects_bearer_auth():
    """/api/me/token is session-cookie only -- a bearer token must not
    satisfy it, unlike the api_router routes which accept either."""
    from aggregator import access_control

    user = await _make_user("me-token-bearer-user")
    token = await access_control.generate_personal_token(user)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/api/me/token", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_auth_providers_reflects_configured_state(client, monkeypatch):
    from aggregator import identity_providers

    monkeypatch.setattr(identity_providers.github_provider, "is_configured", lambda: True)
    monkeypatch.setattr(identity_providers.steam_provider, "is_configured", lambda: False)
    resp = await client.get("/api/auth/providers")
    assert resp.status_code == 200
    assert resp.json() == {"github": True, "steam": False}


async def test_admin_link_route_requires_session(client):
    resp = await client.get("/admin/link/github", follow_redirects=False)
    assert resp.status_code == 401


async def test_admin_link_route_redirects_to_provider_when_authenticated(client):
    user = await _make_user("link-route-user")
    client.cookies.set("admin_session", _session_cookie(user))
    resp = await client.get("/admin/link/github", follow_redirects=False)
    assert resp.status_code == 302
    assert "link_identity_state=" in resp.headers.get("set-cookie", "")


async def test_admin_link_route_rejects_unknown_provider(client):
    user = await _make_user("link-route-unknown-provider-user")
    client.cookies.set("admin_session", _session_cookie(user))
    resp = await client.get("/admin/link/discord", follow_redirects=False)
    assert resp.status_code == 400


async def test_unlink_identity_requires_session(client):
    resp = await client.delete("/api/me/identities/1")
    assert resp.status_code == 401


async def test_unlink_identity_refuses_last_identity(client):
    from aggregator import database

    user = await _make_user("unlink-route-user")
    client.cookies.set("admin_session", _session_cookie(user))
    identities = await database.list_user_identities(int(user.removeprefix("user:")))
    resp = await client.delete(f"/api/me/identities/{identities[0].id}")
    assert resp.status_code == 400


async def test_unlink_identity_succeeds_for_non_last_identity(client):
    from aggregator import access_control, database

    user = await _make_user("unlink-route-multi-user")
    await access_control.link_identity(user, "steam", "76500000000000050", "X")
    client.cookies.set("admin_session", _session_cookie(user))
    identities = await database.list_user_identities(int(user.removeprefix("user:")))
    steam_identity = next(i for i in identities if i.provider == "steam")
    resp = await client.delete(f"/api/me/identities/{steam_identity.id}")
    assert resp.status_code == 200
