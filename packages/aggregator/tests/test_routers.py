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
