"""
Regression tests for the /api/servers REST routes added for editing --
PATCH /servers/{id}. Uses a minimal FastAPI app (just api_router, no
lifespan) over httpx's ASGI transport, matching the auth conventions
require_api_auth expects (Bearer ADMIN_TOKEN, set to "test-admin-token"
by tests/conftest.py before aggregator.config is first imported).
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aggregator.api.routers import router as api_router
from aggregator.child_manager import child_manager
from aggregator.database import delete_server, list_servers

AUTH_HEADERS = {"Authorization": "Bearer test-admin-token"}


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _cleanup_by_name(name: str) -> None:
    if child_manager.get(name):
        await child_manager.remove(name)
    for server in await list_servers():
        if server.name == name:
            await delete_server(server.id)


async def test_patch_updates_only_provided_fields(client):
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
            headers=AUTH_HEADERS,
        )
        server_id = added.json()["server"]["id"]

        resp = await client.patch(
            f"/api/servers/{server_id}", json={"env": {"B": "2"}}, headers=AUTH_HEADERS
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == name
        assert body["package"] == "http://a.invalid/mcp"
        assert body["env"] == {"B": "2"}
    finally:
        await _cleanup_by_name(name)


async def test_patch_rename_conflict_returns_400(client):
    name_a, name_b = "patch-conflict-a", "patch-conflict-b"
    try:
        await client.post(
            "/api/servers",
            json={"name": name_a, "type": "proxy", "package": "http://a.invalid/mcp"},
            headers=AUTH_HEADERS,
        )
        b = await client.post(
            "/api/servers",
            json={"name": name_b, "type": "proxy", "package": "http://b.invalid/mcp"},
            headers=AUTH_HEADERS,
        )
        b_id = b.json()["server"]["id"]

        resp = await client.patch(
            f"/api/servers/{b_id}", json={"name": name_a}, headers=AUTH_HEADERS
        )
        assert resp.status_code == 400
    finally:
        await _cleanup_by_name(name_a)
        await _cleanup_by_name(name_b)


async def test_patch_while_running_restarts_child_under_new_name(client, proxy_target_url):
    old_name, new_name = "patch-running-old", "patch-running-new"
    try:
        added = await client.post(
            "/api/servers",
            json={"name": old_name, "type": "proxy", "package": proxy_target_url},
            headers=AUTH_HEADERS,
        )
        server_id = added.json()["server"]["id"]
        assert child_manager.get(old_name) is not None

        resp = await client.patch(
            f"/api/servers/{server_id}", json={"name": new_name}, headers=AUTH_HEADERS
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


async def test_patch_while_disabled_does_not_touch_child_manager(client):
    name = "patch-disabled"
    try:
        added = await client.post(
            "/api/servers",
            json={"name": name, "type": "proxy", "package": "http://a.invalid/mcp"},
            headers=AUTH_HEADERS,
        )
        server_id = added.json()["server"]["id"]
        await client.post(f"/api/servers/{server_id}/disable", headers=AUTH_HEADERS)
        assert child_manager.get(name) is None

        resp = await client.patch(
            f"/api/servers/{server_id}",
            json={"package": "http://b.invalid/mcp"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is False
        assert body["package"] == "http://b.invalid/mcp"
        assert child_manager.get(name) is None
    finally:
        await _cleanup_by_name(name)


async def test_patch_nonexistent_id_returns_404(client):
    resp = await client.patch(
        "/api/servers/999999999", json={"package": "http://x.invalid/mcp"}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 404


async def test_patch_without_auth_returns_401(client):
    resp = await client.patch("/api/servers/1", json={"package": "http://x.invalid/mcp"})
    assert resp.status_code == 401


async def test_patch_while_running_without_rename_restarts_in_place(client, proxy_target_url):
    name = "patch-running-same-name"
    try:
        added = await client.post(
            "/api/servers",
            json={"name": name, "type": "proxy", "package": proxy_target_url},
            headers=AUTH_HEADERS,
        )
        server_id = added.json()["server"]["id"]
        assert child_manager.get(name) is not None

        resp = await client.patch(
            f"/api/servers/{server_id}", json={"env": {"X": "1"}}, headers=AUTH_HEADERS
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


async def test_patch_noop_does_not_restart_running_child(client, proxy_target_url):
    name = "patch-noop"
    try:
        added = await client.post(
            "/api/servers",
            json={"name": name, "type": "proxy", "package": proxy_target_url},
            headers=AUTH_HEADERS,
        )
        server_id = added.json()["server"]["id"]
        original_session = child_manager.get(name).session

        resp = await client.patch(f"/api/servers/{server_id}", json={}, headers=AUTH_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is True
        assert body["tool_count"] == 2
        assert child_manager.get(name).session is original_session
    finally:
        await _cleanup_by_name(name)


async def test_patch_git_rename_uninstalls_old_checkout(client, monkeypatch):
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
            headers=AUTH_HEADERS,
        )
        server_id = added.json()["server"]["id"]

        resp = await client.patch(
            f"/api/servers/{server_id}", json={"name": new_name}, headers=AUTH_HEADERS
        )
        assert resp.status_code == 200

        assert len(calls) == 1
        assert calls[0].name == old_name
    finally:
        await _cleanup_by_name(old_name)
        await _cleanup_by_name(new_name)


async def test_patch_git_package_only_change_uninstalls_old_checkout(client, monkeypatch):
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
            headers=AUTH_HEADERS,
        )
        server_id = added.json()["server"]["id"]

        resp = await client.patch(
            f"/api/servers/{server_id}",
            json={"package": "git+https://example.invalid/repo-b.git"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert len(calls) == 1
        assert calls[0].name == name
        assert calls[0].package == "git+https://example.invalid/repo-a.git"
    finally:
        await _cleanup_by_name(name)


async def test_patch_git_to_non_git_type_change_uninstalls_old_checkout(client, monkeypatch):
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
            headers=AUTH_HEADERS,
        )
        server_id = added.json()["server"]["id"]

        resp = await client.patch(
            f"/api/servers/{server_id}",
            json={"type": "cmd", "package": "/no/such/binary"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 200
        assert len(calls) == 1
        assert calls[0].name == name
        assert calls[0].type == "git"
    finally:
        await _cleanup_by_name(name)
