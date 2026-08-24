"""
Regression tests for the /api/servers, /api/tools, and /api/logs REST
routes -- editing (PATCH /servers/{id}) plus per-user visibility/ownership
scoping added in docs/superpowers/plans/2026-08-20-per-user-server-access.md.

Uses a minimal FastAPI app (just api_router, no lifespan) over httpx's ASGI
transport. Auth is a real personal token minted via the token_for fixture
(conftest.py), exercising the same access_control.validate_personal_token
path a live personal token would.
"""

import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aggregator import log_capture
from aggregator.api.routers import router as api_router
from aggregator.child_manager import child_manager
from aggregator.database import delete_server, list_servers
from aggregator.log_capture import LogEntry


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def owner(make_user):
    return await make_user("router-owner")


@pytest.fixture
async def stranger(make_user):
    return await make_user("router-stranger")


@pytest.fixture
async def admin(make_user):
    return await make_user("test-admin", is_admin=True)


@pytest.fixture
async def auth_headers(token_for, owner):
    token = await token_for(owner)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def stranger_headers(token_for, stranger):
    token = await token_for(stranger)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def admin_headers(token_for, admin):
    token = await token_for(admin)
    return {"Authorization": f"Bearer {token}"}


async def _cleanup_by_name(name: str) -> None:
    if child_manager.get(name):
        await child_manager.remove(name)
    for server in await list_servers():
        if server.name == name:
            await delete_server(server.id)


async def test_patch_updates_only_provided_fields(client, auth_headers):
    name = "patch-partial"
    try:
        added = await client.post(
            "/api/servers",
            json={
                "name": name,
                "type": "proxy",
                "package": "http://a.invalid/mcp",
                "env": {"A": "1"},
            },
            headers=auth_headers,
        )
        server_id = added.json()["server"]["id"]

        resp = await client.patch(
            f"/api/servers/{server_id}", json={"env": {"B": "2"}}, headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == name
        assert body["package"] == "http://a.invalid/mcp"
        assert body["env"] == {"B": "2"}
    finally:
        await _cleanup_by_name(name)


async def test_patch_rename_conflict_returns_400(client, auth_headers):
    name_a, name_b = "patch-conflict-a", "patch-conflict-b"
    try:
        await client.post(
            "/api/servers",
            json={"name": name_a, "type": "proxy", "package": "http://a.invalid/mcp"},
            headers=auth_headers,
        )
        b = await client.post(
            "/api/servers",
            json={"name": name_b, "type": "proxy", "package": "http://b.invalid/mcp"},
            headers=auth_headers,
        )
        b_id = b.json()["server"]["id"]

        resp = await client.patch(
            f"/api/servers/{b_id}", json={"name": name_a}, headers=auth_headers
        )
        assert resp.status_code == 400
    finally:
        await _cleanup_by_name(name_a)
        await _cleanup_by_name(name_b)


async def test_patch_while_running_restarts_child_under_new_name(
    client, auth_headers, proxy_target_url
):
    old_name, new_name = "patch-running-old", "patch-running-new"
    try:
        added = await client.post(
            "/api/servers",
            json={"name": old_name, "type": "proxy", "package": proxy_target_url},
            headers=auth_headers,
        )
        server_id = added.json()["server"]["id"]
        assert child_manager.get(old_name) is not None

        resp = await client.patch(
            f"/api/servers/{server_id}", json={"name": new_name}, headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is True
        assert body["tool_count"] == 2
        assert body["error"] is None
        assert child_manager.get(old_name) is None
        assert child_manager.get(new_name) is not None
    finally:
        await _cleanup_by_name(old_name)
        await _cleanup_by_name(new_name)


async def test_patch_while_disabled_does_not_touch_child_manager(client, auth_headers):
    name = "patch-disabled"
    try:
        added = await client.post(
            "/api/servers",
            json={"name": name, "type": "proxy", "package": "http://a.invalid/mcp"},
            headers=auth_headers,
        )
        server_id = added.json()["server"]["id"]
        await client.post(f"/api/servers/{server_id}/disable", headers=auth_headers)
        assert child_manager.get(name) is None

        resp = await client.patch(
            f"/api/servers/{server_id}",
            json={"package": "http://b.invalid/mcp"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is False
        assert body["package"] == "http://b.invalid/mcp"
        assert child_manager.get(name) is None
    finally:
        await _cleanup_by_name(name)


async def test_patch_nonexistent_id_returns_404(client, auth_headers):
    resp = await client.patch(
        "/api/servers/999999999", json={"package": "http://x.invalid/mcp"}, headers=auth_headers
    )
    assert resp.status_code == 404


async def test_patch_without_auth_returns_401(client):
    resp = await client.patch("/api/servers/1", json={"package": "http://x.invalid/mcp"})
    assert resp.status_code == 401


async def test_patch_while_running_without_rename_restarts_in_place(
    client, auth_headers, proxy_target_url
):
    name = "patch-running-same-name"
    try:
        added = await client.post(
            "/api/servers",
            json={"name": name, "type": "proxy", "package": proxy_target_url},
            headers=auth_headers,
        )
        server_id = added.json()["server"]["id"]
        assert child_manager.get(name) is not None

        resp = await client.patch(
            f"/api/servers/{server_id}", json={"env": {"X": "1"}}, headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is True
        assert body["tool_count"] == 2
        assert body["error"] is None
        assert body["env"] == {"X": "1"}
        assert child_manager.get(name) is not None
    finally:
        await _cleanup_by_name(name)


async def test_patch_noop_does_not_restart_running_child(client, auth_headers, proxy_target_url):
    name = "patch-noop"
    try:
        added = await client.post(
            "/api/servers",
            json={"name": name, "type": "proxy", "package": proxy_target_url},
            headers=auth_headers,
        )
        server_id = added.json()["server"]["id"]
        original_session = child_manager.get(name).session

        resp = await client.patch(f"/api/servers/{server_id}", json={}, headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is True
        assert body["tool_count"] == 2
        assert child_manager.get(name).session is original_session
    finally:
        await _cleanup_by_name(name)


async def test_patch_git_rename_uninstalls_old_checkout(client, auth_headers, monkeypatch):
    old_name, new_name = "patch-git-old", "patch-git-new"
    calls = []

    async def fake_uninstall(config):
        calls.append(config)

    monkeypatch.setattr("aggregator.api.routers.uninstall", fake_uninstall)
    try:
        added = await client.post(
            "/api/servers",
            json={
                "name": old_name,
                "type": "git",
                "package": "git+https://example.invalid/repo.git",
            },
            headers=auth_headers,
        )
        server_id = added.json()["server"]["id"]

        resp = await client.patch(
            f"/api/servers/{server_id}", json={"name": new_name}, headers=auth_headers
        )
        assert resp.status_code == 200

        assert len(calls) == 1
        assert calls[0].name == old_name
    finally:
        await _cleanup_by_name(old_name)
        await _cleanup_by_name(new_name)


async def test_patch_git_package_only_change_uninstalls_old_checkout(
    client, auth_headers, monkeypatch
):
    name = "patch-git-pkg-change"
    calls = []

    async def fake_uninstall(config):
        calls.append(config)

    monkeypatch.setattr("aggregator.api.routers.uninstall", fake_uninstall)
    try:
        added = await client.post(
            "/api/servers",
            json={
                "name": name,
                "type": "git",
                "package": "git+https://example.invalid/repo-a.git",
            },
            headers=auth_headers,
        )
        server_id = added.json()["server"]["id"]

        resp = await client.patch(
            f"/api/servers/{server_id}",
            json={"package": "git+https://example.invalid/repo-b.git"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert len(calls) == 1
        assert calls[0].name == name
        assert calls[0].package == "git+https://example.invalid/repo-a.git"
    finally:
        await _cleanup_by_name(name)


async def test_patch_git_to_non_git_type_change_uninstalls_old_checkout(
    client, auth_headers, monkeypatch
):
    name = "patch-git-type-change"
    calls = []

    async def fake_uninstall(config):
        calls.append(config)

    monkeypatch.setattr("aggregator.api.routers.uninstall", fake_uninstall)
    try:
        added = await client.post(
            "/api/servers",
            json={
                "name": name,
                "type": "git",
                "package": "git+https://example.invalid/repo.git",
            },
            headers=auth_headers,
        )
        server_id = added.json()["server"]["id"]

        resp = await client.patch(
            f"/api/servers/{server_id}",
            json={"type": "cmd", "package": "/no/such/binary"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert len(calls) == 1
        assert calls[0].name == name
        assert calls[0].type == "git"
    finally:
        await _cleanup_by_name(name)


# ── Visibility / ownership scoping ────────────────────────────────────────────


async def test_add_server_defaults_to_private_and_records_owner(client, auth_headers, owner):
    name = "router-visibility-default"
    try:
        added = await client.post(
            "/api/servers",
            json={"name": name, "type": "proxy", "package": "http://a.invalid/mcp"},
            headers=auth_headers,
        )
        body = added.json()["server"]
        assert body["visibility"] == "private"
        assert body["owner"] == owner
    finally:
        await _cleanup_by_name(name)


async def test_list_servers_hides_private_servers_from_other_users(
    client, auth_headers, stranger_headers
):
    name = "router-visibility-hidden"
    try:
        await client.post(
            "/api/servers",
            json={
                "name": name,
                "type": "proxy",
                "package": "http://a.invalid/mcp",
                "visibility": "private",
            },
            headers=auth_headers,
        )
        owner_list = await client.get("/api/servers", headers=auth_headers)
        stranger_list = await client.get("/api/servers", headers=stranger_headers)
        assert any(s["name"] == name for s in owner_list.json())
        assert all(s["name"] != name for s in stranger_list.json())
    finally:
        await _cleanup_by_name(name)


async def test_list_servers_shows_everyone_visibility_to_all(
    client, auth_headers, stranger_headers
):
    name = "router-visibility-shared"
    try:
        await client.post(
            "/api/servers",
            json={
                "name": name,
                "type": "proxy",
                "package": "http://a.invalid/mcp",
                "visibility": "everyone",
            },
            headers=auth_headers,
        )
        stranger_list = await client.get("/api/servers", headers=stranger_headers)
        assert any(s["name"] == name for s in stranger_list.json())
    finally:
        await _cleanup_by_name(name)


async def test_stranger_gets_404_managing_owners_private_server(
    client, auth_headers, stranger_headers
):
    name = "router-visibility-manage-denied"
    try:
        added = await client.post(
            "/api/servers",
            json={
                "name": name,
                "type": "proxy",
                "package": "http://a.invalid/mcp",
                "visibility": "private",
            },
            headers=auth_headers,
        )
        server_id = added.json()["server"]["id"]

        patch_resp = await client.patch(
            f"/api/servers/{server_id}",
            json={"package": "http://b.invalid/mcp"},
            headers=stranger_headers,
        )
        delete_resp = await client.delete(f"/api/servers/{server_id}", headers=stranger_headers)
        assert patch_resp.status_code == 404
        assert delete_resp.status_code == 404
    finally:
        await _cleanup_by_name(name)


async def test_admin_can_flip_visibility_on_any_users_server(client, auth_headers, admin_headers):
    name = "router-visibility-admin-flip"
    try:
        added = await client.post(
            "/api/servers",
            json={
                "name": name,
                "type": "proxy",
                "package": "http://a.invalid/mcp",
                "visibility": "private",
            },
            headers=auth_headers,
        )
        server_id = added.json()["server"]["id"]

        resp = await client.patch(
            f"/api/servers/{server_id}", json={"visibility": "everyone"}, headers=admin_headers
        )
        assert resp.status_code == 200
        assert resp.json()["visibility"] == "everyone"
    finally:
        await _cleanup_by_name(name)


async def test_tools_list_scoped_to_visible_servers(
    client, auth_headers, stranger_headers, proxy_target_url
):
    name = "router-tools-scoped"
    try:
        await client.post(
            "/api/servers",
            json={
                "name": name,
                "type": "proxy",
                "package": proxy_target_url,
                "visibility": "private",
            },
            headers=auth_headers,
        )
        owner_tools = await client.get("/api/tools", headers=auth_headers)
        stranger_tools = await client.get("/api/tools", headers=stranger_headers)
        assert any(t["server"] == name for t in owner_tools.json())
        assert all(t["server"] != name for t in stranger_tools.json())
    finally:
        await _cleanup_by_name(name)


async def test_tools_call_rejects_inaccessible_server(
    client, auth_headers, stranger_headers, proxy_target_url
):
    name = "router-tools-call-scoped"
    try:
        await client.post(
            "/api/servers",
            json={
                "name": name,
                "type": "proxy",
                "package": proxy_target_url,
                "visibility": "private",
            },
            headers=auth_headers,
        )
        resp = await client.post(
            "/api/tools/call",
            json={"server": name, "tool": "echo", "arguments": {"text": "hi"}},
            headers=stranger_headers,
        )
        assert resp.status_code == 404
    finally:
        await _cleanup_by_name(name)


async def test_logs_stderr_rejects_inaccessible_server(client, auth_headers, stranger_headers):
    name = "router-logs-scoped"
    try:
        await client.post(
            "/api/servers",
            json={
                "name": name,
                "type": "proxy",
                "package": "http://a.invalid/mcp",
                "visibility": "private",
            },
            headers=auth_headers,
        )
        owner_resp = await client.get(f"/api/logs/{name}/stderr", headers=auth_headers)
        stranger_resp = await client.get(f"/api/logs/{name}/stderr", headers=stranger_headers)
        assert owner_resp.status_code == 200
        assert stranger_resp.status_code == 404
    finally:
        await _cleanup_by_name(name)


async def test_logs_get_rejects_inaccessible_explicit_server(
    client, auth_headers, stranger_headers
):
    name = "router-logs-explicit-scoped"
    try:
        await client.post(
            "/api/servers",
            json={
                "name": name,
                "type": "proxy",
                "package": "http://a.invalid/mcp",
                "visibility": "private",
            },
            headers=auth_headers,
        )
        resp = await client.get("/api/logs", params={"server": name}, headers=stranger_headers)
        assert resp.status_code == 404
    finally:
        await _cleanup_by_name(name)


async def test_logs_get_no_server_param_excludes_private_entries_from_stranger(
    client, auth_headers, stranger_headers
):
    name = "router-logs-unscoped"
    try:
        await client.post(
            "/api/servers",
            json={
                "name": name,
                "type": "proxy",
                "package": "http://a.invalid/mcp",
                "visibility": "private",
            },
            headers=auth_headers,
        )
        log_capture._append(
            LogEntry(ts=time.time(), level="INFO", server=name, msg="private-server-log-entry")
        )

        owner_resp = await client.get("/api/logs", headers=auth_headers)
        stranger_resp = await client.get("/api/logs", headers=stranger_headers)
        assert owner_resp.status_code == 200
        assert stranger_resp.status_code == 200
        assert any(e["server"] == name for e in owner_resp.json())
        assert all(e["server"] != name for e in stranger_resp.json())
    finally:
        await _cleanup_by_name(name)


async def test_logs_stream_rejects_inaccessible_explicit_server(
    client, auth_headers, stranger_headers
):
    name = "router-logs-stream-scoped"
    try:
        await client.post(
            "/api/servers",
            json={
                "name": name,
                "type": "proxy",
                "package": "http://a.invalid/mcp",
                "visibility": "private",
            },
            headers=auth_headers,
        )
        resp = await client.get(
            "/api/logs/stream", params={"server": name}, headers=stranger_headers
        )
        assert resp.status_code == 404
    finally:
        await _cleanup_by_name(name)
