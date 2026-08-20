"""
Regression tests for aggregator.database.update_server -- the partial-field
update used by both PATCH /servers/{id} and the edit_server meta-tool.
"""

import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from aggregator.database import (
    _migrate_server_columns,
    add_server,
    delete_server,
    get_username_by_token_hash,
    set_personal_token,
    update_server,
)
from aggregator.models import ServerType, ServerVisibility


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
        with pytest.raises(ValueError, match="edit-db-conflict-a"):
            await update_server(b.id, name="edit-db-conflict-a")
    finally:
        await _cleanup(a.id)
        await _cleanup(b.id)


async def test_update_server_unknown_id_returns_none():
    assert await update_server(999_999_999, name="whatever") is None


async def test_add_server_defaults_to_private_visibility_and_no_owner():
    server = await add_server("model-default-visibility", ServerType.PROXY, "http://x.invalid/mcp")
    try:
        assert server.visibility == "private"
        assert server.owner_username is None
    finally:
        await delete_server(server.id)


async def test_add_server_stores_explicit_owner_and_visibility():
    server = await add_server(
        "model-explicit-visibility",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username="alice",
        visibility=ServerVisibility.EVERYONE.value,
    )
    try:
        assert server.owner_username == "alice"
        assert server.visibility == "everyone"
    finally:
        await delete_server(server.id)


async def test_update_server_can_change_visibility_only():
    server = await add_server(
        "model-update-visibility",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        visibility=ServerVisibility.PRIVATE.value,
    )
    try:
        updated = await update_server(server.id, visibility=ServerVisibility.EVERYONE.value)
        assert updated.visibility == "everyone"
        assert updated.name == "model-update-visibility"  # untouched
    finally:
        await delete_server(server.id)


async def test_set_and_get_personal_token_by_hash():
    await set_personal_token("token-model-user", "hash-a")
    assert await get_username_by_token_hash("hash-a") == "token-model-user"
    assert await get_username_by_token_hash("no-such-hash") is None


async def test_set_personal_token_replaces_previous_hash_for_same_user():
    await set_personal_token("token-model-user-2", "hash-old")
    await set_personal_token("token-model-user-2", "hash-new")
    assert await get_username_by_token_hash("hash-old") is None
    assert await get_username_by_token_hash("hash-new") == "token-model-user-2"


async def test_migrate_server_columns_backfills_legacy_table(tmp_path):
    """A `servers` table created before this feature shipped has no
    owner_username/visibility columns. `SQLModel.metadata.create_all` only
    creates missing tables, never alters an existing one -- so init_db()
    needs this explicit step, verified here in isolation against a
    standalone legacy-shaped DB file, not the shared test-session DB."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE servers ("
        "id INTEGER PRIMARY KEY, name TEXT UNIQUE, type TEXT, package TEXT, "
        "args TEXT DEFAULT '[]', env TEXT DEFAULT '{}', enabled BOOLEAN DEFAULT 1)"
    )
    conn.execute(
        "INSERT INTO servers (name, type, package) VALUES "
        "('legacy-server', 'proxy', 'http://x.invalid/mcp')"
    )
    conn.commit()
    conn.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await _migrate_server_columns(conn)

        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT owner_username, visibility FROM servers WHERE name='legacy-server'")
            )
            row = result.fetchone()
        assert row == (None, "everyone")
    finally:
        await engine.dispose()
