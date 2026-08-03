"""
Regression tests for the native meta MCP tools (list/add/delete/enable/
disable/restart), see docs/superpowers/plans/2026-08-02-meta-tools.md --
previously verified only via one-off scratch scripts.
"""

import json

import pytest

from aggregator import meta_tools
from aggregator.child_manager import child_manager
from aggregator.database import delete_server, list_servers


def _payload(result: list) -> dict | list:
    return json.loads(result[0].text)


async def _cleanup_by_name(name: str) -> None:
    if child_manager.get(name):
        await child_manager.remove(name)
    for server in await list_servers():
        if server.name == name:
            await delete_server(server.id)


async def test_add_list_enable_disable_restart_delete_round_trip(proxy_target_url):
    name = "meta-round-trip"
    try:
        added = _payload(
            await meta_tools.call(
                "add_server",
                {"name": name, "type": "proxy", "package": proxy_target_url},
            )
        )
        assert added["error"] is None
        assert set(added["tools"]) == {"echo", "add"}

        listed = _payload(await meta_tools.call("list_servers", {}))
        entry = next(s for s in listed if s["name"] == name)
        assert entry["running"] is True
        assert entry["tool_count"] == 2
        assert entry["error"] is None

        off = _payload(await meta_tools.call("disable_server", {"name": name}))
        assert off == {"name": name, "enabled": False}
        assert child_manager.get(name) is None

        on = _payload(await meta_tools.call("enable_server", {"name": name}))
        assert on["enabled"] is True
        assert on["tool_count"] == 2

        restarted = _payload(await meta_tools.call("restart_server", {"name": name}))
        assert restarted == {"name": name, "tool_count": 2}

        deleted = _payload(await meta_tools.call("delete_server", {"name": name}))
        assert deleted == {"deleted": name}

        listed_after = _payload(await meta_tools.call("list_servers", {}))
        assert all(s["name"] != name for s in listed_after)
    finally:
        await _cleanup_by_name(name)


async def test_env_values_redacted_in_list_servers(proxy_target_url):
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
        )
        listed = _payload(await meta_tools.call("list_servers", {}))
        entry = next(s for s in listed if s["name"] == name)
        assert entry["env"] == {"API_KEY": "***"}
    finally:
        await _cleanup_by_name(name)


async def test_action_on_unknown_server_name_raises_value_error():
    with pytest.raises(ValueError, match="No server named"):
        await meta_tools.call("restart_server", {"name": "does-not-exist"})


async def test_add_server_with_invalid_type_raises_value_error():
    with pytest.raises(ValueError):
        await meta_tools.call(
            "add_server",
            {"name": "x", "type": "not-a-real-type", "package": "y"},
        )
