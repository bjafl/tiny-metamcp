"""Tests for api/users_router.py's admin-only user/allowed-identity management."""

import pytest
from httpx import ASGITransport, AsyncClient

from aggregator import access_control, database
from aggregator.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _session_cookie(username: str) -> str:
    from aggregator import admin_auth

    return admin_auth._signer.dumps({"username": username, "display_name": username})


async def _make_user(raw_id: str, *, is_admin: bool = False) -> str:
    canonical = await access_control.resolve_login("github", raw_id, raw_id)
    if is_admin:
        await database.update_user_flags(int(canonical.removeprefix("user:")), is_admin=True)
    return canonical


async def test_list_users_requires_admin(client):
    user = await _make_user("users-router-nonadmin")
    client.cookies.set("admin_session", _session_cookie(user))
    resp = await client.get("/api/users")
    assert resp.status_code == 403


async def test_list_users_returns_users_for_admin(client):
    admin = await _make_user("users-router-admin", is_admin=True)
    user = await _make_user("users-router-listed")
    client.cookies.set("admin_session", _session_cookie(admin))
    resp = await client.get("/api/users")
    assert resp.status_code == 200
    ids = {u["id"] for u in resp.json()}
    assert int(admin.removeprefix("user:")) in ids
    assert int(user.removeprefix("user:")) in ids


async def test_update_user_toggles_admin_flag(client):
    admin = await _make_user("users-router-toggler", is_admin=True)
    target = await _make_user("users-router-target")
    client.cookies.set("admin_session", _session_cookie(admin))
    target_id = int(target.removeprefix("user:"))
    resp = await client.patch(f"/api/users/{target_id}", json={"is_admin": True})
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is True


async def test_update_user_cannot_remove_own_admin_rights(client):
    admin = await _make_user("users-router-self-demote", is_admin=True)
    client.cookies.set("admin_session", _session_cookie(admin))
    admin_id = int(admin.removeprefix("user:"))
    resp = await client.patch(f"/api/users/{admin_id}", json={"is_admin": False})
    assert resp.status_code == 400


async def test_update_user_cannot_disable_own_account(client):
    admin = await _make_user("users-router-self-disable", is_admin=True)
    client.cookies.set("admin_session", _session_cookie(admin))
    admin_id = int(admin.removeprefix("user:"))
    resp = await client.patch(f"/api/users/{admin_id}", json={"allowed": False})
    assert resp.status_code == 400


async def test_update_user_unknown_id_returns_404(client):
    admin = await _make_user("users-router-404-admin", is_admin=True)
    client.cookies.set("admin_session", _session_cookie(admin))
    resp = await client.patch("/api/users/999999999", json={"is_admin": True})
    assert resp.status_code == 404


async def test_allowed_identities_crud_requires_admin(client):
    admin = await _make_user("users-router-allowlist-admin", is_admin=True)
    client.cookies.set("admin_session", _session_cookie(admin))

    resp = await client.post(
        "/api/allowed-identities",
        json={"provider": "github", "raw_id": "future-user-xyz", "grant_admin": False},
    )
    assert resp.status_code == 201
    row_id = resp.json()["id"]

    resp = await client.get("/api/allowed-identities")
    assert resp.status_code == 200
    assert any(r["id"] == row_id for r in resp.json())

    resp = await client.delete(f"/api/allowed-identities/{row_id}")
    assert resp.status_code == 200

    resp = await client.get("/api/allowed-identities")
    assert not any(r["id"] == row_id for r in resp.json())


async def test_allowed_identities_list_excludes_consumed_rows(client):
    admin = await _make_user("users-router-consumed-admin", is_admin=True)
    client.cookies.set("admin_session", _session_cookie(admin))

    resp = await client.post(
        "/api/allowed-identities",
        json={"provider": "github", "raw_id": "consumed-allowed-user", "grant_admin": False},
    )
    assert resp.status_code == 201
    row_id = resp.json()["id"]

    # Consume the row via a real login -- resolve_login no longer deletes
    # it (Finding 1), so the admin-facing "pending" list must filter it
    # out instead.
    await access_control.resolve_login("github", "consumed-allowed-user", "Consumed")

    resp = await client.get("/api/allowed-identities")
    assert resp.status_code == 200
    assert not any(r["id"] == row_id for r in resp.json())

    await database.delete_allowed_identity(row_id)


async def test_add_allowed_identity_rejects_blank_raw_id(client):
    admin = await _make_user("users-router-blank-raw-id-admin", is_admin=True)
    client.cookies.set("admin_session", _session_cookie(admin))
    resp = await client.post(
        "/api/allowed-identities",
        json={"provider": "github", "raw_id": "   ", "grant_admin": False},
    )
    assert resp.status_code == 400


async def test_add_allowed_identity_rejects_unknown_provider(client):
    admin = await _make_user("users-router-bad-provider-admin", is_admin=True)
    client.cookies.set("admin_session", _session_cookie(admin))
    resp = await client.post(
        "/api/allowed-identities", json={"provider": "discord", "raw_id": "x", "grant_admin": False}
    )
    assert resp.status_code == 400


async def test_add_allowed_identity_rejects_duplicate(client):
    admin = await _make_user("users-router-dup-admin", is_admin=True)
    client.cookies.set("admin_session", _session_cookie(admin))
    body = {"provider": "github", "raw_id": "dup-allowed-user", "grant_admin": False}
    first = await client.post("/api/allowed-identities", json=body)
    assert first.status_code == 201
    second = await client.post("/api/allowed-identities", json=body)
    assert second.status_code == 400
    await client.delete(f"/api/allowed-identities/{first.json()['id']}")
