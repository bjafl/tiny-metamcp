"""
Tests for aggregator.database: update_server's partial-field update (used
by both PATCH /servers/{id} and the edit_server meta-tool), add_server's
visibility/owner defaults, personal-token CRUD (set/get by hash), and the
_migrate_server_columns SQLite column-backfill migration.
"""

import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from aggregator.database import (
    _migrate_identity_prefixes,
    _migrate_oauth_tokens_table,
    _migrate_server_columns,
    add_server,
    create_allowed_identity,
    create_user,
    create_user_identity,
    delete_allowed_identity,
    delete_server,
    delete_user_identity,
    get_allowed_identity,
    get_user,
    get_user_identity,
    get_username_by_token_hash,
    has_any_allowed_identity,
    list_allowed_identities,
    list_pending_allowed_identities,
    list_user_identities,
    list_users,
    set_personal_token,
    update_server,
    update_user_flags,
    update_user_identity_display_name,
)
from aggregator.models import (
    AllowedIdentity,
    ServerType,
    ServerVisibility,
    User,
    UserIdentity,
)


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


async def test_migrate_oauth_tokens_table_drops_legacy_shaped_table(tmp_path):
    """oauth_tokens.github_user was renamed to `username` when Steam login
    was added. These are short-lived (<=30 day) session/refresh tokens, not
    durable data, so the migration just drops a legacy-shaped table -- the
    subsequent create_all() in init_db() recreates it with the new schema.
    Verified here against a standalone legacy-shaped DB file, matching the
    pattern _migrate_server_columns's own test already uses."""
    db_path = tmp_path / "legacy_oauth.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE oauth_tokens ("
        "token TEXT PRIMARY KEY, token_type TEXT, github_user TEXT, "
        "client_id TEXT, expires_at REAL, created_at REAL)"
    )
    conn.execute(
        "INSERT INTO oauth_tokens VALUES "
        "('tok', 'access', 'octocat', 'client-1', 9999999999.0, 0.0)"
    )
    conn.commit()
    conn.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await _migrate_oauth_tokens_table(conn)

        def _table_exists(sync_conn) -> bool:
            from sqlalchemy import inspect

            return "oauth_tokens" in inspect(sync_conn).get_table_names()

        async with engine.connect() as conn:
            exists = await conn.run_sync(_table_exists)
        assert not exists  # dropped; create_all() would recreate it fresh
    finally:
        await engine.dispose()


async def test_migrate_oauth_tokens_table_leaves_new_shaped_table_alone(tmp_path):
    """A table that already has the new `username` column (or no table at
    all -- a fresh install) must not be touched."""
    db_path = tmp_path / "current_oauth.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE oauth_tokens ("
        "token TEXT PRIMARY KEY, token_type TEXT, username TEXT, "
        "client_id TEXT, expires_at REAL, created_at REAL)"
    )
    conn.execute(
        "INSERT INTO oauth_tokens VALUES "
        "('tok', 'access', 'github:octocat', 'client-1', 9999999999.0, 0.0)"
    )
    conn.commit()
    conn.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await _migrate_oauth_tokens_table(conn)

        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT username FROM oauth_tokens"))
            row = result.fetchone()
        assert row == ("github:octocat",)
    finally:
        await engine.dispose()


async def test_migrate_identity_prefixes_backfills_unprefixed_values(tmp_path):
    """Pre-Steam-login deployments' servers.owner_username and
    personal_tokens.username hold bare GitHub logins (GitHub was the only
    provider). After upgrade the session identity is prefixed
    ("github:octocat"), so these bare values must be backfilled with the
    "github:" prefix or every existing owner/token holder loses access."""
    db_path = tmp_path / "unprefixed.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE servers ("
        "id INTEGER PRIMARY KEY, name TEXT UNIQUE, type TEXT, package TEXT, "
        "args TEXT DEFAULT '[]', env TEXT DEFAULT '{}', enabled BOOLEAN DEFAULT 1, "
        "owner_username TEXT, visibility TEXT DEFAULT 'everyone')"
    )
    conn.execute(
        "INSERT INTO servers (name, type, package, owner_username) VALUES "
        "('owned-server', 'proxy', 'http://x.invalid/mcp', 'octocat')"
    )
    conn.execute(
        "INSERT INTO servers (name, type, package, owner_username) VALUES "
        "('unowned-server', 'proxy', 'http://y.invalid/mcp', NULL)"
    )
    conn.execute(
        "CREATE TABLE personal_tokens ("
        "username TEXT PRIMARY KEY, token_hash TEXT UNIQUE, created_at REAL)"
    )
    conn.execute(
        "INSERT INTO personal_tokens (username, token_hash, created_at) VALUES "
        "('octocat', 'hash-1', 0.0)"
    )
    conn.commit()
    conn.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await _migrate_identity_prefixes(conn)

        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT name, owner_username FROM servers ORDER BY name")
            )
            rows = dict(result.fetchall())
            token_result = await conn.execute(text("SELECT username FROM personal_tokens"))
            token_row = token_result.fetchone()
        assert rows["owned-server"] == "github:octocat"
        assert rows["unowned-server"] is None
        assert token_row == ("github:octocat",)
    finally:
        await engine.dispose()


async def test_migrate_identity_prefixes_leaves_already_prefixed_values_alone(tmp_path):
    """A row already holding a prefixed identity (either "steam:..." from a
    fresh Steam login, or an already-migrated "github:...") must not be
    touched again -- the migration must not double-prefix."""
    db_path = tmp_path / "prefixed.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE servers ("
        "id INTEGER PRIMARY KEY, name TEXT UNIQUE, type TEXT, package TEXT, "
        "args TEXT DEFAULT '[]', env TEXT DEFAULT '{}', enabled BOOLEAN DEFAULT 1, "
        "owner_username TEXT, visibility TEXT DEFAULT 'everyone')"
    )
    conn.execute(
        "INSERT INTO servers (name, type, package, owner_username) VALUES "
        "('steam-owned-server', 'proxy', 'http://x.invalid/mcp', 'steam:76561198012345678')"
    )
    conn.execute(
        "CREATE TABLE personal_tokens ("
        "username TEXT PRIMARY KEY, token_hash TEXT UNIQUE, created_at REAL)"
    )
    conn.execute(
        "INSERT INTO personal_tokens (username, token_hash, created_at) VALUES "
        "('github:octocat', 'hash-1', 0.0)"
    )
    conn.commit()
    conn.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await _migrate_identity_prefixes(conn)

        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT owner_username FROM servers WHERE name='steam-owned-server'")
            )
            owner = result.scalar_one()
            token_result = await conn.execute(text("SELECT username FROM personal_tokens"))
            token_row = token_result.fetchone()
        assert owner == "steam:76561198012345678"
        assert token_row == ("github:octocat",)
    finally:
        await engine.dispose()


async def test_user_and_identity_tables_round_trip():
    """Smoke test: the new tables exist (created by init_db()'s
    create_all(), already run once for the whole session by conftest.py's
    _init_db fixture) and a row written through the ORM reads back
    unchanged."""
    from aggregator.database import _session_factory

    async with _session_factory() as session:
        user = User(is_admin=True)
        session.add(user)
        await session.commit()
        await session.refresh(user)

        identity = UserIdentity(
            user_id=user.id, provider="github", raw_id="model-test-user", display_name="Tester"
        )
        session.add(identity)
        await session.commit()
        await session.refresh(identity)

        allowed = AllowedIdentity(provider="steam", raw_id="76500000000000001", grant_admin=False)
        session.add(allowed)
        await session.commit()
        await session.refresh(allowed)

    assert user.id is not None
    assert user.allowed is True  # default
    assert identity.id is not None
    assert identity.user_id == user.id
    assert allowed.id is not None


async def test_user_identity_unique_constraint_rejects_duplicate_provider_raw_id():
    from sqlalchemy.exc import IntegrityError

    from aggregator.database import _session_factory

    async with _session_factory() as session:
        user = User()
        session.add(user)
        await session.commit()
        await session.refresh(user)
        session.add(UserIdentity(user_id=user.id, provider="github", raw_id="dup-test"))
        await session.commit()

    async with _session_factory() as session:
        session.add(UserIdentity(user_id=user.id, provider="github", raw_id="dup-test"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_migrate_to_user_accounts_converts_prefixed_strings(tmp_path):
    """A pre-account-linking deployment has "provider:raw" strings directly
    in servers.owner_username / personal_tokens.username. This migration
    must convert each distinct value into a real User + UserIdentity and
    rewrite the column to "user:<id>" -- verified here against a standalone
    legacy-shaped DB file, matching the pattern this file's other migration
    tests already use."""
    from aggregator.database import _migrate_to_user_accounts

    db_path = tmp_path / "legacy_prefixed.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, is_admin BOOLEAN, allowed BOOLEAN, "
        "created_at REAL)"
    )
    conn.execute(
        "CREATE TABLE user_identities (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "provider TEXT, raw_id TEXT, display_name TEXT)"
    )
    conn.execute(
        "CREATE TABLE servers (id INTEGER PRIMARY KEY, name TEXT UNIQUE, type TEXT, "
        "package TEXT, owner_username TEXT)"
    )
    conn.execute(
        "CREATE TABLE personal_tokens (username TEXT PRIMARY KEY, token_hash TEXT, created_at REAL)"
    )
    conn.execute(
        "INSERT INTO servers (name, type, package, owner_username) VALUES "
        "('legacy-owned', 'proxy', 'http://x.invalid/mcp', 'github:octocat')"
    )
    conn.execute(
        "INSERT INTO servers (name, type, package, owner_username) VALUES "
        "('legacy-unowned', 'proxy', 'http://y.invalid/mcp', NULL)"
    )
    conn.execute(
        "INSERT INTO personal_tokens (username, token_hash) VALUES "
        "('github:octocat', 'hash-1')"  # same identity as the server owner above
    )
    conn.commit()
    conn.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await _migrate_to_user_accounts(conn)

        async with engine.connect() as conn:
            owned = (
                await conn.execute(
                    text("SELECT owner_username FROM servers WHERE name = 'legacy-owned'")
                )
            ).fetchone()[0]
            unowned = (
                await conn.execute(
                    text("SELECT owner_username FROM servers WHERE name = 'legacy-unowned'")
                )
            ).fetchone()[0]
            token_username = (
                await conn.execute(text("SELECT username FROM personal_tokens"))
            ).fetchone()[0]
            identity_row = (
                await conn.execute(
                    text("SELECT provider, raw_id FROM user_identities WHERE raw_id = 'octocat'")
                )
            ).fetchone()

        assert owned.startswith("user:")
        assert unowned is None  # untouched
        # Same underlying identity was used in both tables -- must resolve
        # to the SAME new user, not two different ones.
        assert token_username == owned
        assert identity_row == ("github", "octocat")
    finally:
        await engine.dispose()


async def test_migrate_to_user_accounts_is_idempotent(tmp_path):
    """Running the migration twice (e.g. a container restart mid-upgrade)
    must not create duplicate users or double-prefix already-migrated
    values."""
    from aggregator.database import _migrate_to_user_accounts

    db_path = tmp_path / "idempotent.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, is_admin BOOLEAN, allowed BOOLEAN, "
        "created_at REAL)"
    )
    conn.execute(
        "CREATE TABLE user_identities (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "provider TEXT, raw_id TEXT, display_name TEXT)"
    )
    conn.execute(
        "CREATE TABLE servers (id INTEGER PRIMARY KEY, name TEXT UNIQUE, type TEXT, "
        "package TEXT, owner_username TEXT)"
    )
    conn.execute(
        "CREATE TABLE personal_tokens (username TEXT PRIMARY KEY, token_hash TEXT, created_at REAL)"
    )
    conn.execute(
        "INSERT INTO servers (name, type, package, owner_username) VALUES "
        "('idempotent-server', 'proxy', 'http://x.invalid/mcp', 'github:octocat')"
    )
    conn.commit()
    conn.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await _migrate_to_user_accounts(conn)
        async with engine.begin() as conn:
            await _migrate_to_user_accounts(conn)  # run again

        async with engine.connect() as conn:
            user_count = (await conn.execute(text("SELECT COUNT(*) FROM users"))).fetchone()[0]
            owner = (
                await conn.execute(
                    text("SELECT owner_username FROM servers WHERE name = 'idempotent-server'")
                )
            ).fetchone()[0]
        assert user_count == 1
        assert owner.startswith("user:") and owner.count(":") == 1
    finally:
        await engine.dispose()


async def test_seed_auth_env_vars_creates_allowed_identities_and_admins(tmp_path, monkeypatch):
    from aggregator import database

    monkeypatch.setattr(database, "GITHUB_ALLOWED_USERS", {"octocat"})
    monkeypatch.setattr(database, "STEAM_ALLOWED_USERS", set())
    monkeypatch.setattr(database, "ADMIN_USERS", {"github:root-admin"})

    db_path = tmp_path / "seed.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, is_admin BOOLEAN, allowed BOOLEAN, "
        "created_at REAL)"
    )
    conn.execute(
        "CREATE TABLE user_identities (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "provider TEXT, raw_id TEXT, display_name TEXT)"
    )
    conn.execute(
        "CREATE TABLE allowed_identities (id INTEGER PRIMARY KEY, provider TEXT, "
        "raw_id TEXT, grant_admin BOOLEAN)"
    )
    conn.execute("CREATE TABLE auth_seed_state (id INTEGER PRIMARY KEY, seeded BOOLEAN)")
    conn.commit()
    conn.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await database._seed_auth_env_vars(conn)

        async with engine.connect() as conn:
            allowed_rows = (
                await conn.execute(text("SELECT provider, raw_id FROM allowed_identities"))
            ).fetchall()
            admin_row = (
                await conn.execute(
                    text(
                        "SELECT u.is_admin FROM users u "
                        "JOIN user_identities ui ON ui.user_id = u.id "
                        "WHERE ui.provider = 'github' AND ui.raw_id = 'root-admin'"
                    )
                )
            ).fetchone()
            seeded = (
                await conn.execute(text("SELECT seeded FROM auth_seed_state WHERE id = 1"))
            ).fetchone()[0]

        assert ("github", "octocat") in allowed_rows
        assert admin_row == (1,)
        assert seeded == 1
    finally:
        await engine.dispose()


async def test_seed_auth_env_vars_runs_only_once(tmp_path, monkeypatch):
    """A second call (e.g. next container restart) must not re-seed, even
    if the env vars still contain values -- otherwise an admin who
    deliberately emptied allowed_identities would have it silently
    repopulated from a stale .env."""
    from aggregator import database

    monkeypatch.setattr(database, "GITHUB_ALLOWED_USERS", {"octocat"})
    monkeypatch.setattr(database, "STEAM_ALLOWED_USERS", set())
    monkeypatch.setattr(database, "ADMIN_USERS", set())

    db_path = tmp_path / "seed_once.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, is_admin BOOLEAN, allowed BOOLEAN, "
        "created_at REAL)"
    )
    conn.execute(
        "CREATE TABLE user_identities (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "provider TEXT, raw_id TEXT, display_name TEXT)"
    )
    conn.execute(
        "CREATE TABLE allowed_identities (id INTEGER PRIMARY KEY, provider TEXT, "
        "raw_id TEXT, grant_admin BOOLEAN)"
    )
    conn.execute("CREATE TABLE auth_seed_state (id INTEGER PRIMARY KEY, seeded BOOLEAN)")
    conn.commit()
    conn.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await database._seed_auth_env_vars(conn)

        # Admin deliberately empties the allow-list.
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM allowed_identities"))

        async with engine.begin() as conn:
            await database._seed_auth_env_vars(conn)  # runs again on next restart

        async with engine.connect() as conn:
            count = (
                await conn.execute(text("SELECT COUNT(*) FROM allowed_identities"))
            ).fetchone()[0]
        assert count == 0  # stayed empty -- not silently repopulated
    finally:
        await engine.dispose()


async def test_create_and_get_user():
    user = await create_user(is_admin=True)
    fetched = await get_user(user.id)
    assert fetched is not None
    assert fetched.is_admin is True
    assert fetched.allowed is True


async def test_get_user_unknown_id_returns_none():
    assert await get_user(999_999_999) is None


async def test_list_users_includes_created_user():
    user = await create_user()
    names = {u.id for u in await list_users()}
    assert user.id in names


async def test_update_user_flags_partial_update():
    user = await create_user(is_admin=False)
    updated = await update_user_flags(user.id, allowed=False)
    assert updated.allowed is False
    assert updated.is_admin is False  # untouched


async def test_update_user_flags_unknown_id_returns_none():
    assert await update_user_flags(999_999_999, is_admin=True) is None


async def test_create_and_get_user_identity():
    user = await create_user()
    identity = await create_user_identity(user.id, "github", "crud-test-user", "Tester")
    fetched = await get_user_identity("github", "crud-test-user")
    assert fetched is not None
    assert fetched.id == identity.id
    assert fetched.user_id == user.id


async def test_get_user_identity_unknown_returns_none():
    assert await get_user_identity("github", "no-such-user-xyz") is None


async def test_update_user_identity_display_name():
    user = await create_user()
    identity = await create_user_identity(user.id, "steam", "76500000000000002", "Old Name")
    await update_user_identity_display_name(identity.id, "New Name")
    fetched = await get_user_identity("steam", "76500000000000002")
    assert fetched.display_name == "New Name"


async def test_list_and_delete_user_identity():
    user = await create_user()
    identity = await create_user_identity(user.id, "github", "crud-list-test", None)
    assert identity.id in {i.id for i in await list_user_identities(user.id)}
    await delete_user_identity(identity.id)
    assert identity.id not in {i.id for i in await list_user_identities(user.id)}


async def test_allowed_identity_crud_round_trip():
    row = await create_allowed_identity("github", "crud-allowed-test", grant_admin=True)
    fetched = await get_allowed_identity("github", "crud-allowed-test")
    assert fetched is not None
    assert fetched.grant_admin is True
    assert row.id in {r.id for r in await list_allowed_identities()}
    await delete_allowed_identity(row.id)
    assert await get_allowed_identity("github", "crud-allowed-test") is None


async def test_has_any_allowed_identity_true_and_false():
    assert not await has_any_allowed_identity("discord")  # unused provider, definitely empty
    row = await create_allowed_identity("discord", "has-any-test")
    try:
        assert await has_any_allowed_identity("discord")
    finally:
        await delete_allowed_identity(row.id)


async def test_list_pending_allowed_identities_excludes_consumed_rows():
    row1 = await create_allowed_identity("github", "pending-test-unused")
    row2 = await create_allowed_identity("github", "pending-test-used")
    user = await create_user()
    await create_user_identity(user.id, "github", "pending-test-used", None)
    try:
        pending = await list_pending_allowed_identities()
        pending_pairs = {(r.provider, r.raw_id) for r in pending}
        assert ("github", "pending-test-unused") in pending_pairs
        assert ("github", "pending-test-used") not in pending_pairs
    finally:
        await delete_allowed_identity(row1.id)
        await delete_allowed_identity(row2.id)
