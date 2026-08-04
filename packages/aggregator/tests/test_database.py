"""
Regression tests for aggregator.database.update_server -- the partial-field
update used by both PATCH /servers/{id} and the edit_server meta-tool.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from aggregator.database import add_server, delete_server, update_server
from aggregator.models import ServerType


async def _cleanup(server_id: int) -> None:
    await delete_server(server_id)


async def test_update_server_partial_field_only_changes_that_field():
    server = await add_server(
        "edit-db-partial", ServerType.PROXY, "http://example.invalid/mcp", env={"A": "1"}
    )
    try:
        updated = await update_server(server.id, env={"B": "2"})
        assert updated is not None
        assert updated.id == server.id
        assert updated.name == "edit-db-partial"
        assert updated.type == ServerType.PROXY.value
        assert updated.package == "http://example.invalid/mcp"
        assert updated.get_env() == {"B": "2"}
        assert updated.get_args() == []
    finally:
        await _cleanup(server.id)


async def test_update_server_replaces_env_wholesale_not_merged():
    server = await add_server(
        "edit-db-wholesale",
        ServerType.PROXY,
        "http://example.invalid/mcp",
        env={"A": "1", "B": "2"},
    )
    try:
        updated = await update_server(server.id, env={"C": "3"})
        assert updated.get_env() == {"C": "3"}
    finally:
        await _cleanup(server.id)


async def test_update_server_rename_and_type_and_package_together():
    server = await add_server("edit-db-rename-old", ServerType.PROXY, "http://a.invalid/mcp")
    try:
        updated = await update_server(
            server.id,
            name="edit-db-rename-new",
            server_type=ServerType.PROXY,
            package="http://b.invalid/mcp",
        )
        assert updated.name == "edit-db-rename-new"
        assert updated.package == "http://b.invalid/mcp"
    finally:
        await _cleanup(server.id)


async def test_update_server_rename_to_existing_name_raises():
    a = await add_server("edit-db-conflict-a", ServerType.PROXY, "http://a.invalid/mcp")
    b = await add_server("edit-db-conflict-b", ServerType.PROXY, "http://b.invalid/mcp")
    try:
        with pytest.raises(IntegrityError):
            await update_server(b.id, name="edit-db-conflict-a")
    finally:
        await _cleanup(a.id)
        await _cleanup(b.id)


async def test_update_server_unknown_id_returns_none():
    assert await update_server(999_999_999, name="whatever") is None
