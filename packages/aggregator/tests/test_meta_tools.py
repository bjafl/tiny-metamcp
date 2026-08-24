"""
Regression tests for the native meta MCP tools (list/add/delete/enable/
disable/restart), see docs/superpowers/plans/2026-08-02-meta-tools.md --
previously verified only via one-off scratch scripts. Extended in
docs/superpowers/plans/2026-08-20-per-user-server-access.md to cover
per-user visibility/ownership scoping.
"""

import json

import pytest

from aggregator import meta_tools
from aggregator.child_manager import child_manager
from aggregator.database import delete_server, list_servers


@pytest.fixture
async def user(make_user):
    return await make_user("meta-test-user")


@pytest.fixture
async def owner(make_user):
    return await make_user("meta-owner")


@pytest.fixture
async def stranger(make_user):
    return await make_user("meta-stranger")


@pytest.fixture
async def admin(make_user):
    return await make_user("test-admin", is_admin=True)


def _payload(result: list) -> dict | list:
    return json.loads(result[0].text)


async def _cleanup_by_name(name: str) -> None:
    if child_manager.get(name):
        await child_manager.remove(name)
    for server in await list_servers():
        if server.name == name:
            await delete_server(server.id)


async def test_add_list_enable_disable_restart_delete_round_trip(proxy_target_url, user):
    name = "meta-round-trip"
    try:
        added = _payload(
            await meta_tools.call(
                "add_server",
                {"name": name, "type": "proxy", "package": proxy_target_url},
                user,
            )
        )
        assert added["error"] is None
        assert set(added["tools"]) == {"echo", "add"}

        listed = _payload(await meta_tools.call("list_servers", {}, user))
        entry = next(s for s in listed if s["name"] == name)
        assert entry["running"] is True
        assert entry["tool_count"] == 2
        assert entry["error"] is None

        off = _payload(await meta_tools.call("disable_server", {"name": name}, user))
        assert off == {"name": name, "enabled": False}
        assert child_manager.get(name) is None

        on = _payload(await meta_tools.call("enable_server", {"name": name}, user))
        assert on["enabled"] is True
        assert on["tool_count"] == 2

        restarted = _payload(await meta_tools.call("restart_server", {"name": name}, user))
        assert restarted == {"name": name, "tool_count": 2}

        deleted = _payload(await meta_tools.call("delete_server", {"name": name}, user))
        assert deleted == {"deleted": name}

        listed_after = _payload(await meta_tools.call("list_servers", {}, user))
        assert all(s["name"] != name for s in listed_after)
    finally:
        await _cleanup_by_name(name)


async def test_env_values_redacted_in_list_servers(proxy_target_url, user):
    name = "meta-env-redact"
    try:
        await meta_tools.call(
            "add_server",
            {
                "name": name,
                "type": "proxy",
                "package": proxy_target_url,
                "env": {"API_KEY": "super-secret-value"},
            },
            user,
        )
        listed = _payload(await meta_tools.call("list_servers", {}, user))
        entry = next(s for s in listed if s["name"] == name)
        assert entry["env"] == {"API_KEY": "***"}
    finally:
        await _cleanup_by_name(name)


async def test_action_on_unknown_server_name_raises_value_error(user):
    with pytest.raises(ValueError, match="No server named"):
        await meta_tools.call("restart_server", {"name": "does-not-exist"}, user)


async def test_add_server_with_invalid_type_raises_value_error(user):
    with pytest.raises(ValueError):
        await meta_tools.call(
            "add_server",
            {"name": "x", "type": "not-a-real-type", "package": "y"},
            user,
        )


async def test_edit_server_updates_only_provided_fields(proxy_target_url, user):
    name = "meta-edit-partial"
    try:
        await meta_tools.call(
            "add_server",
            {"name": name, "type": "proxy", "package": proxy_target_url, "env": {"A": "1"}},
            user,
        )
        edited = _payload(
            await meta_tools.call("edit_server", {"name": name, "env": {"B": "2"}}, user)
        )
        assert edited["error"] is None
        assert edited["server"]["package"] == proxy_target_url
        assert edited["server"]["env"] == {"B": "***"}
    finally:
        await _cleanup_by_name(name)


async def test_edit_server_rename_moves_child_manager_key(proxy_target_url, user):
    old_name, new_name = "meta-edit-rename-old", "meta-edit-rename-new"
    try:
        await meta_tools.call(
            "add_server", {"name": old_name, "type": "proxy", "package": proxy_target_url}, user
        )
        assert child_manager.get(old_name) is not None

        edited = _payload(
            await meta_tools.call("edit_server", {"name": old_name, "new_name": new_name}, user)
        )
        assert edited["server"]["name"] == new_name
        assert set(edited["tools"]) == {"echo", "add"}
        assert child_manager.get(old_name) is None
        assert child_manager.get(new_name) is not None
    finally:
        await _cleanup_by_name(old_name)
        await _cleanup_by_name(new_name)


async def test_edit_server_unknown_name_raises_value_error(user):
    with pytest.raises(ValueError, match="No server named"):
        await meta_tools.call("edit_server", {"name": "does-not-exist", "package": "x"}, user)


async def test_edit_server_invalid_type_raises_value_error(proxy_target_url, user):
    name = "meta-edit-bad-type"
    try:
        await meta_tools.call(
            "add_server", {"name": name, "type": "proxy", "package": proxy_target_url}, user
        )
        with pytest.raises(ValueError):
            await meta_tools.call("edit_server", {"name": name, "type": "not-a-real-type"}, user)
    finally:
        await _cleanup_by_name(name)


async def test_edit_server_while_running_without_rename_restarts_in_place(proxy_target_url, user):
    name = "meta-edit-same-name"
    try:
        await meta_tools.call(
            "add_server", {"name": name, "type": "proxy", "package": proxy_target_url}, user
        )
        assert child_manager.get(name) is not None

        edited = _payload(
            await meta_tools.call("edit_server", {"name": name, "env": {"X": "1"}}, user)
        )
        assert edited["error"] is None
        assert set(edited["tools"]) == {"echo", "add"}
        assert edited["server"]["env"] == {"X": "***"}
        assert child_manager.get(name) is not None
    finally:
        await _cleanup_by_name(name)


async def test_edit_server_noop_does_not_restart_running_child(proxy_target_url, user):
    name = "meta-edit-noop"
    try:
        await meta_tools.call(
            "add_server", {"name": name, "type": "proxy", "package": proxy_target_url}, user
        )
        original_session = child_manager.get(name).session

        edited = _payload(await meta_tools.call("edit_server", {"name": name}, user))
        assert edited["error"] is None
        assert set(edited["tools"]) == {"echo", "add"}
        assert child_manager.get(name).session is original_session
    finally:
        await _cleanup_by_name(name)


async def test_edit_server_git_rename_uninstalls_old_checkout(monkeypatch, user):
    old_name, new_name = "meta-edit-git-old", "meta-edit-git-new"
    calls = []

    async def fake_uninstall(config):
        calls.append(config)

    monkeypatch.setattr("aggregator.meta_tools.uninstall", fake_uninstall)
    try:
        await meta_tools.call(
            "add_server",
            {
                "name": old_name,
                "type": "git",
                "package": "git+https://example.invalid/repo.git",
            },
            user,
        )

        edited = _payload(
            await meta_tools.call("edit_server", {"name": old_name, "new_name": new_name}, user)
        )
        assert edited["server"]["name"] == new_name

        assert len(calls) == 1
        assert calls[0].name == old_name
    finally:
        await _cleanup_by_name(old_name)
        await _cleanup_by_name(new_name)


async def test_edit_server_git_package_only_change_uninstalls_old_checkout(monkeypatch, user):
    name = "meta-edit-git-pkg-change"
    calls = []

    async def fake_uninstall(config):
        calls.append(config)

    monkeypatch.setattr("aggregator.meta_tools.uninstall", fake_uninstall)
    try:
        await meta_tools.call(
            "add_server",
            {"name": name, "type": "git", "package": "git+https://example.invalid/repo-a.git"},
            user,
        )

        edited = _payload(
            await meta_tools.call(
                "edit_server",
                {"name": name, "package": "git+https://example.invalid/repo-b.git"},
                user,
            )
        )
        assert edited["server"]["package"] == "git+https://example.invalid/repo-b.git"

        assert len(calls) == 1
        assert calls[0].name == name
        assert calls[0].package == "git+https://example.invalid/repo-a.git"
    finally:
        await _cleanup_by_name(name)


async def test_edit_server_git_to_non_git_type_change_uninstalls_old_checkout(monkeypatch, user):
    name = "meta-edit-git-type-change"
    calls = []

    async def fake_uninstall(config):
        calls.append(config)

    monkeypatch.setattr("aggregator.meta_tools.uninstall", fake_uninstall)
    try:
        await meta_tools.call(
            "add_server",
            {"name": name, "type": "git", "package": "git+https://example.invalid/repo.git"},
            user,
        )

        edited = _payload(
            await meta_tools.call(
                "edit_server", {"name": name, "type": "cmd", "package": "/no/such/binary"}, user
            )
        )
        assert edited["server"]["type"] == "cmd"

        assert len(calls) == 1
        assert calls[0].name == name
        assert calls[0].type == "git"
    finally:
        await _cleanup_by_name(name)


# ── Visibility / ownership scoping ────────────────────────────────────────────


async def test_add_server_defaults_to_private_visibility(proxy_target_url, owner):
    name = "meta-visibility-default"
    try:
        added = _payload(
            await meta_tools.call(
                "add_server",
                {"name": name, "type": "proxy", "package": proxy_target_url},
                owner,
            )
        )
        assert added["server"]["visibility"] == "private"
        assert added["server"]["owner"] == owner
    finally:
        await _cleanup_by_name(name)


async def test_list_servers_hides_private_servers_from_other_users(
    proxy_target_url, owner, stranger
):
    name = "meta-visibility-hidden"
    try:
        await meta_tools.call(
            "add_server",
            {
                "name": name,
                "type": "proxy",
                "package": proxy_target_url,
                "visibility": "private",
            },
            owner,
        )
        owner_view = _payload(await meta_tools.call("list_servers", {}, owner))
        stranger_view = _payload(await meta_tools.call("list_servers", {}, stranger))
        assert any(s["name"] == name for s in owner_view)
        assert all(s["name"] != name for s in stranger_view)
    finally:
        await _cleanup_by_name(name)


async def test_list_servers_shows_everyone_visibility_to_all_users(
    proxy_target_url, owner, stranger
):
    name = "meta-visibility-shared"
    try:
        await meta_tools.call(
            "add_server",
            {
                "name": name,
                "type": "proxy",
                "package": proxy_target_url,
                "visibility": "everyone",
            },
            owner,
        )
        stranger_view = _payload(await meta_tools.call("list_servers", {}, stranger))
        assert any(s["name"] == name for s in stranger_view)
    finally:
        await _cleanup_by_name(name)


async def test_stranger_cannot_manage_owners_private_server(proxy_target_url, owner, stranger):
    name = "meta-visibility-manage-denied"
    try:
        await meta_tools.call(
            "add_server",
            {
                "name": name,
                "type": "proxy",
                "package": proxy_target_url,
                "visibility": "private",
            },
            owner,
        )
        with pytest.raises(ValueError, match="No server named"):
            await meta_tools.call("restart_server", {"name": name}, stranger)
        with pytest.raises(ValueError, match="No server named"):
            await meta_tools.call("delete_server", {"name": name}, stranger)
    finally:
        await _cleanup_by_name(name)


async def test_admin_can_manage_any_users_private_server(proxy_target_url, owner, admin):
    name = "meta-visibility-admin-manage"
    try:
        await meta_tools.call(
            "add_server",
            {
                "name": name,
                "type": "proxy",
                "package": proxy_target_url,
                "visibility": "private",
            },
            owner,
        )
        restarted = _payload(await meta_tools.call("restart_server", {"name": name}, admin))
        assert restarted == {"name": name, "tool_count": 2}
    finally:
        await _cleanup_by_name(name)
