# Account Linking + Admin-Editable User Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a person log in with either GitHub or Steam and land on the same account (self-service linking), and move allow-lists/admin rights from env-vars to a DB-backed model editable from the webui.

**Architecture:** Two new tables (`users`, `user_identities`) replace the old "one prefixed string = one account" model with a canonical, provider-independent `"user:<id>"` identity that `Server.owner_username`/`PersonalToken.username`/the session cookie now hold. A third table (`allowed_identities`) is the DB-backed, admin-editable replacement for `GITHUB_ALLOWED_USERS`/`STEAM_ALLOWED_USERS`/`ADMIN_USERS`, which become one-time seed values only. `access_control.py` gains an async `resolve_login()` (provider identity → canonical account, auto-provisioning on first login) and every existing access check (`is_admin`, `can_manage`, `_is_visible`, `validate_personal_token`) moves from a synchronous in-memory check to an async DB lookup, preserving the existing "re-check on every request" security posture.

**Tech Stack:** FastAPI, SQLModel/SQLAlchemy (async, SQLite), itsdangerous (signed cookies), React + TanStack Query/Router (webui).

**Spec:** docs/superpowers/specs/2026-08-24-account-linking-admin-management-design.md

## Global Constraints

- Canonical identity format: `"user:<id>"` (e.g. `"user:42"`), provider-independent. `Server.owner_username`, `PersonalToken.username`, and the session cookie's `"username"` field all hold this from now on — no column type change, still `str`.
- At most one `UserIdentity` per `(provider, raw_id)` pair (DB-enforced via a unique constraint). At most one identity per provider per user is enforced at the application layer, not the schema.
- `AllowedIdentity` empty for a given provider = unrestricted for that provider (identical to today's `GITHUB_ALLOWED_USERS`/`STEAM_ALLOWED_USERS` empty-set semantics) — preserved for upgrade continuity.
- `GITHUB_ALLOWED_USERS`/`STEAM_ALLOWED_USERS`/`ADMIN_USERS` env vars are consulted **exactly once**, gated by a dedicated `auth_seed_state` marker row — never by table emptiness. After that, they have zero runtime effect.
- Self-service linking only — no admin-forced merge of two existing accounts. Unlinking is allowed but refuses to remove an account's last remaining identity.
- Un-configuring a provider stops **new** logins and links through it, but does not retroactively invalidate an already-authenticated session/token for an account that also has another, still-configured identity linked (see spec's "Documented behavior change" note). The account-level `allowed` toggle is the ongoing, per-request revocation mechanism now.
- No caching layer — every request re-checks current DB state, consistent with this project's existing security posture and its low-QPS, self-hosted deployment target.
- `uv run`/`uvx` from `packages/aggregator/`; `pnpm` from `packages/webui/`.
- Migration order in `database.init_db()`: `_migrate_oauth_tokens_table` → `create_all` → `_migrate_server_columns` → `_migrate_identity_prefixes` (existing) → `_migrate_to_user_accounts` (new) → `_seed_auth_env_vars` (new). Each step's guaranteed output shape is the next step's precondition — do not reorder.

---

### Task 1: Data model — User, UserIdentity, AllowedIdentity, AuthSeedState

**Files:**
- Modify: `packages/aggregator/src/aggregator/models.py`
- Test: `packages/aggregator/tests/test_database.py`

**Interfaces:**
- Produces: `User(id, is_admin, allowed, created_at)`, `UserIdentity(id, user_id, provider, raw_id, display_name)` (unique on `(provider, raw_id)`), `AllowedIdentity(id, provider, raw_id, grant_admin)` (unique on `(provider, raw_id)`), `AuthSeedState(id, seeded)` — all `SQLModel(table=True)`, all later tasks import these from `aggregator.models`.

- [ ] **Step 1: Write a failing test that the new tables exist and round-trip**

Add to `packages/aggregator/tests/test_database.py` (near the top, after the existing imports):

```python
from aggregator.models import AllowedIdentity, AuthSeedState, User, UserIdentity


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
```

Also add `import pytest` at the top of `test_database.py` if not already present (check first — it may already be imported for other tests in this file).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd packages/aggregator && uv run pytest tests/test_database.py -k "user_and_identity or unique_constraint" -v`
Expected: FAIL with `ImportError: cannot import name 'User' from 'aggregator.models'` (or similar — the models don't exist yet).

- [ ] **Step 3: Add the models**

In `packages/aggregator/src/aggregator/models.py`, add `from sqlalchemy import UniqueConstraint` to the imports at the top, then append these four classes after the existing `PersonalToken` class:

```python
class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    is_admin: bool = Field(default=False)
    allowed: bool = Field(default=True)  # single account-wide revoke switch
    created_at: float = Field(default_factory=_time.time)


class UserIdentity(SQLModel, table=True):
    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint("provider", "raw_id", name="uq_user_identities_provider_raw_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    provider: str  # "github" / "steam"
    raw_id: str  # unprefixed provider-native id (GitHub login / SteamID64)
    display_name: str | None = Field(default=None)  # last-seen persona name


class AllowedIdentity(SQLModel, table=True):
    __tablename__ = "allowed_identities"
    __table_args__ = (
        UniqueConstraint("provider", "raw_id", name="uq_allowed_identities_provider_raw_id"),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider: str
    raw_id: str
    grant_admin: bool = Field(default=False)


class AuthSeedState(SQLModel, table=True):
    __tablename__ = "auth_seed_state"

    id: int = Field(default=1, primary_key=True)
    seeded: bool = Field(default=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd packages/aggregator && uv run pytest tests/test_database.py -k "user_and_identity or unique_constraint" -v`
Expected: PASS (2 tests). Note: these tests rely on `create_all()` having already created the new tables — conftest.py's session-scoped `_init_db` fixture calls `init_db()` once, which (as of Task 1) doesn't yet run any *new* migration function, but `create_all()` itself already picks up every `SQLModel` subclass with `table=True` automatically, so the tables exist without any further wiring.

- [ ] **Step 5: Commit**

```bash
cd packages/aggregator
git add src/aggregator/models.py tests/test_database.py
git commit -m "feat(aggregator): add User/UserIdentity/AllowedIdentity/AuthSeedState models"
```

---

### Task 2: Migrations — convert to user accounts, seed env vars once

**Files:**
- Modify: `packages/aggregator/src/aggregator/database.py`
- Test: `packages/aggregator/tests/test_database.py`

**Interfaces:**
- Consumes: `User`, `UserIdentity`, `AllowedIdentity`, `AuthSeedState` (Task 1). `GITHUB_ALLOWED_USERS`, `STEAM_ALLOWED_USERS`, `ADMIN_USERS` from `.config` (unchanged, already exist).
- Produces: `_migrate_to_user_accounts(conn)`, `_seed_auth_env_vars(conn)` — both wired into `init_db()`. Later tasks (3+) rely on the invariant that after `init_db()` runs, every `servers.owner_username`/`personal_tokens.username` value still present is either `NULL`/empty or shaped `"user:<id>"`.

- [ ] **Step 1: Write failing tests for `_migrate_to_user_accounts`**

Add to `packages/aggregator/tests/test_database.py`:

```python
async def test_migrate_to_user_accounts_converts_prefixed_strings(tmp_path):
    """A pre-account-linking deployment has "provider:raw" strings directly
    in servers.owner_username / personal_tokens.username. This migration
    must convert each distinct value into a real User + UserIdentity and
    rewrite the column to "user:<id>" -- verified here against a standalone
    legacy-shaped DB file, matching the pattern this file's other migration
    tests already use."""
    import sqlite3

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

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
        "CREATE TABLE personal_tokens (username TEXT PRIMARY KEY, token_hash TEXT, "
        "created_at REAL)"
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
    import sqlite3

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

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
        "CREATE TABLE personal_tokens (username TEXT PRIMARY KEY, token_hash TEXT, "
        "created_at REAL)"
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_database.py -k migrate_to_user_accounts -v`
Expected: FAIL with `ImportError: cannot import name '_migrate_to_user_accounts'`.

- [ ] **Step 3: Implement `_migrate_to_user_accounts`**

In `packages/aggregator/src/aggregator/database.py`, add `import time` to the top-level imports (alongside the existing `import json`). Then add this function after `_migrate_identity_prefixes`:

```python
async def _migrate_to_user_accounts(conn: AsyncConnection) -> None:
    """Pre-account-linking deployments store a raw "provider:raw_id" string
    directly in servers.owner_username / personal_tokens.username. Convert
    each distinct value into a real User + UserIdentity, then rewrite the
    column to the new canonical "user:<id>" form. Pure format conversion --
    no two distinct old values are ever merged into the same account here;
    merging only ever happens through the self-service linking flow from
    now on (access_control.link_identity). Idempotent: a value already
    shaped "user:<digits>" is left untouched, so re-running this on an
    already-migrated deployment or a fresh install is a no-op. Must run
    after create_all() and _migrate_server_columns()/_migrate_identity_prefixes()
    -- both tables must already have their final column set, and any bare
    (non-prefixed) legacy value must already have been prefixed."""

    def _sync(sync_conn):
        now = time.time()
        seen: dict[tuple[str, str], int] = {}

        def _canonical_for(value: str | None) -> str | None:
            if not value or not value.strip():
                return value
            if value.startswith("user:") and value.removeprefix("user:").isdigit():
                return value  # already migrated
            provider, sep, raw_id = value.partition(":")
            if not sep:
                return value  # not a "provider:raw" shape -- nothing sane to do, leave it
            key = (provider, raw_id)
            if key not in seen:
                result = sync_conn.execute(
                    text("INSERT INTO users (is_admin, allowed, created_at) VALUES (0, 1, :now)"),
                    {"now": now},
                )
                user_id = result.lastrowid
                sync_conn.execute(
                    text(
                        "INSERT INTO user_identities (user_id, provider, raw_id, display_name) "
                        "VALUES (:user_id, :provider, :raw_id, NULL)"
                    ),
                    {"user_id": user_id, "provider": provider, "raw_id": raw_id},
                )
                seen[key] = user_id
            return f"user:{seen[key]}"

        for server_id, owner in sync_conn.execute(
            text("SELECT id, owner_username FROM servers WHERE owner_username IS NOT NULL")
        ).fetchall():
            new_value = _canonical_for(owner)
            if new_value != owner:
                sync_conn.execute(
                    text("UPDATE servers SET owner_username = :v WHERE id = :id"),
                    {"v": new_value, "id": server_id},
                )

        for (old_username,) in sync_conn.execute(
            text("SELECT username FROM personal_tokens")
        ).fetchall():
            new_value = _canonical_for(old_username)
            if new_value != old_username:
                sync_conn.execute(
                    text("UPDATE personal_tokens SET username = :v WHERE username = :old"),
                    {"v": new_value, "old": old_username},
                )

    await conn.run_sync(_sync)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_database.py -k migrate_to_user_accounts -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Write failing tests for `_seed_auth_env_vars`**

Add to `packages/aggregator/tests/test_database.py`:

```python
async def test_seed_auth_env_vars_creates_allowed_identities_and_admins(tmp_path, monkeypatch):
    import sqlite3

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

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
    import sqlite3

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

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
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_database.py -k seed_auth_env_vars -v`
Expected: FAIL with `AttributeError: module 'aggregator.database' has no attribute '_seed_auth_env_vars'`.

- [ ] **Step 7: Implement `_seed_auth_env_vars` and wire both migrations into `init_db()`**

In `packages/aggregator/src/aggregator/database.py`, add to the `.config` import line (currently `from .config import DB_PATH`):

```python
from .config import ADMIN_USERS, DB_PATH, GITHUB_ALLOWED_USERS, STEAM_ALLOWED_USERS
```

Add this function after `_migrate_to_user_accounts`:

```python
async def _seed_auth_env_vars(conn: AsyncConnection) -> None:
    """GITHUB_ALLOWED_USERS/STEAM_ALLOWED_USERS/ADMIN_USERS are now only
    ever consulted here, exactly once -- after this runs, allow-lists and
    admin rights live entirely in the allowed_identities/users tables,
    managed from the webui (api/users_router.py). Gated by a dedicated
    auth_seed_state row, not table emptiness: an admin who deliberately
    empties allowed_identities later must not have it silently
    repopulated by a stale .env on the next restart. Must run after
    _migrate_to_user_accounts() -- an ADMIN_USERS entry that already has a
    User (because they already owned a server or held a personal token)
    gets is_admin=1 set directly rather than a duplicate account created."""

    def _sync(sync_conn):
        row = sync_conn.execute(text("SELECT seeded FROM auth_seed_state WHERE id = 1")).fetchone()
        if row is None:
            sync_conn.execute(text("INSERT INTO auth_seed_state (id, seeded) VALUES (1, 0)"))
        elif row[0]:
            return  # already seeded -- these three env vars are now inert

        now = time.time()

        for raw_id in GITHUB_ALLOWED_USERS:
            sync_conn.execute(
                text(
                    "INSERT OR IGNORE INTO allowed_identities (provider, raw_id, grant_admin) "
                    "VALUES ('github', :raw_id, 0)"
                ),
                {"raw_id": raw_id},
            )
        for raw_id in STEAM_ALLOWED_USERS:
            sync_conn.execute(
                text(
                    "INSERT OR IGNORE INTO allowed_identities (provider, raw_id, grant_admin) "
                    "VALUES ('steam', :raw_id, 0)"
                ),
                {"raw_id": raw_id},
            )

        for entry in ADMIN_USERS:
            provider, sep, raw_id = entry.partition(":")
            if not sep:
                continue  # malformed (missing prefix) -- skip, matches the old default-deny
            existing = sync_conn.execute(
                text("SELECT user_id FROM user_identities WHERE provider = :p AND raw_id = :r"),
                {"p": provider, "r": raw_id},
            ).fetchone()
            if existing is not None:
                sync_conn.execute(
                    text("UPDATE users SET is_admin = 1 WHERE id = :id"), {"id": existing[0]}
                )
            else:
                result = sync_conn.execute(
                    text("INSERT INTO users (is_admin, allowed, created_at) VALUES (1, 1, :now)"),
                    {"now": now},
                )
                new_user_id = result.lastrowid
                sync_conn.execute(
                    text(
                        "INSERT INTO user_identities (user_id, provider, raw_id, display_name) "
                        "VALUES (:user_id, :provider, :raw_id, NULL)"
                    ),
                    {"user_id": new_user_id, "provider": provider, "raw_id": raw_id},
                )

        sync_conn.execute(text("UPDATE auth_seed_state SET seeded = 1 WHERE id = 1"))

    await conn.run_sync(_sync)
```

Update `init_db()` to:

```python
async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with _engine.begin() as conn:
        await _migrate_oauth_tokens_table(conn)
        await conn.run_sync(SQLModel.metadata.create_all)
        await _migrate_server_columns(conn)
        await _migrate_identity_prefixes(conn)
        await _migrate_to_user_accounts(conn)
        await _seed_auth_env_vars(conn)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_database.py -v`
Expected: PASS, all tests in the file (existing + new).

- [ ] **Step 9: Commit**

```bash
cd packages/aggregator
git add src/aggregator/database.py tests/test_database.py
git commit -m "feat(aggregator): migrate legacy identities to user accounts, seed allow-lists once"
```

---

### Task 3: Database CRUD helpers for users, identities, allow-list

**Files:**
- Modify: `packages/aggregator/src/aggregator/database.py`
- Test: `packages/aggregator/tests/test_database.py`

**Interfaces:**
- Consumes: `User`, `UserIdentity`, `AllowedIdentity` models (Task 1).
- Produces (all async, all used by Task 4+): `create_user(is_admin=False) -> User`, `get_user(user_id) -> User | None`, `list_users() -> list[User]`, `update_user_flags(user_id, *, is_admin=None, allowed=None) -> User | None`, `get_user_identity(provider, raw_id) -> UserIdentity | None`, `create_user_identity(user_id, provider, raw_id, display_name) -> UserIdentity`, `update_user_identity_display_name(identity_id, display_name) -> None`, `list_user_identities(user_id) -> list[UserIdentity]`, `delete_user_identity(identity_id) -> None`, `get_allowed_identity(provider, raw_id) -> AllowedIdentity | None`, `has_any_allowed_identity(provider) -> bool`, `create_allowed_identity(provider, raw_id, grant_admin=False) -> AllowedIdentity`, `delete_allowed_identity(allowed_identity_id) -> None`, `list_allowed_identities() -> list[AllowedIdentity]`.

- [ ] **Step 1: Write failing tests**

Add to `packages/aggregator/tests/test_database.py`:

```python
from aggregator.database import (
    create_allowed_identity,
    create_user,
    create_user_identity,
    delete_allowed_identity,
    delete_user_identity,
    get_allowed_identity,
    get_user,
    get_user_identity,
    has_any_allowed_identity,
    list_allowed_identities,
    list_user_identities,
    list_users,
    update_user_flags,
    update_user_identity_display_name,
)


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_database.py -k "create_and_get_user or list_users_includes or update_user_flags or create_and_get_user_identity or get_user_identity_unknown or update_user_identity_display_name or list_and_delete_user_identity or allowed_identity_crud or has_any_allowed_identity" -v`
Expected: FAIL with `ImportError` (functions don't exist yet).

- [ ] **Step 3: Implement the CRUD helpers**

In `packages/aggregator/src/aggregator/database.py`, add `AllowedIdentity, AuthSeedState, User, UserIdentity` to the existing `.models` import block (the one with the `# noqa: F401` comment). Then append these sections at the end of the file:

```python
# ── Users ─────────────────────────────────────────────────────────────────────


async def create_user(is_admin: bool = False) -> User:
    async with _session_factory() as session:
        user = User(is_admin=is_admin)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def get_user(user_id: int) -> User | None:
    async with _session_factory() as session:
        return await session.get(User, user_id)


async def list_users() -> list[User]:
    async with _session_factory() as session:
        result = await session.execute(select(User).order_by(User.id))
        return list(result.scalars().all())


async def update_user_flags(
    user_id: int, *, is_admin: bool | None = None, allowed: bool | None = None
) -> User | None:
    async with _session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            return None
        if is_admin is not None:
            user.is_admin = is_admin
        if allowed is not None:
            user.allowed = allowed
        await session.commit()
        await session.refresh(user)
        return user


# ── User identities ──────────────────────────────────────────────────────────


async def get_user_identity(provider: str, raw_id: str) -> UserIdentity | None:
    async with _session_factory() as session:
        result = await session.execute(
            select(UserIdentity).where(
                UserIdentity.provider == provider, UserIdentity.raw_id == raw_id
            )
        )
        return result.scalar_one_or_none()


async def create_user_identity(
    user_id: int, provider: str, raw_id: str, display_name: str | None
) -> UserIdentity:
    async with _session_factory() as session:
        identity = UserIdentity(
            user_id=user_id, provider=provider, raw_id=raw_id, display_name=display_name
        )
        session.add(identity)
        await session.commit()
        await session.refresh(identity)
    return identity


async def update_user_identity_display_name(identity_id: int, display_name: str | None) -> None:
    async with _session_factory() as session:
        identity = await session.get(UserIdentity, identity_id)
        if identity:
            identity.display_name = display_name
            await session.commit()


async def list_user_identities(user_id: int) -> list[UserIdentity]:
    async with _session_factory() as session:
        result = await session.execute(
            select(UserIdentity).where(UserIdentity.user_id == user_id).order_by(UserIdentity.id)
        )
        return list(result.scalars().all())


async def delete_user_identity(identity_id: int) -> None:
    async with _session_factory() as session:
        identity = await session.get(UserIdentity, identity_id)
        if identity:
            await session.delete(identity)
            await session.commit()


# ── Allowed identities (pre-approval list) ──────────────────────────────────


async def get_allowed_identity(provider: str, raw_id: str) -> AllowedIdentity | None:
    async with _session_factory() as session:
        result = await session.execute(
            select(AllowedIdentity).where(
                AllowedIdentity.provider == provider, AllowedIdentity.raw_id == raw_id
            )
        )
        return result.scalar_one_or_none()


async def has_any_allowed_identity(provider: str) -> bool:
    async with _session_factory() as session:
        result = await session.execute(
            select(AllowedIdentity.id).where(AllowedIdentity.provider == provider).limit(1)
        )
        return result.first() is not None


async def create_allowed_identity(
    provider: str, raw_id: str, grant_admin: bool = False
) -> AllowedIdentity:
    async with _session_factory() as session:
        row = AllowedIdentity(provider=provider, raw_id=raw_id, grant_admin=grant_admin)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def delete_allowed_identity(allowed_identity_id: int) -> None:
    async with _session_factory() as session:
        row = await session.get(AllowedIdentity, allowed_identity_id)
        if row:
            await session.delete(row)
            await session.commit()


async def list_allowed_identities() -> list[AllowedIdentity]:
    async with _session_factory() as session:
        result = await session.execute(select(AllowedIdentity).order_by(AllowedIdentity.id))
        return list(result.scalars().all())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_database.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Commit**

```bash
cd packages/aggregator
git add src/aggregator/database.py tests/test_database.py
git commit -m "feat(aggregator): add CRUD helpers for users, identities, allowed_identities"
```

---

### Task 4: `access_control.resolve_login` — login-time identity resolution

**Files:**
- Modify: `packages/aggregator/src/aggregator/access_control.py`
- Test: `packages/aggregator/tests/test_access_control.py`

**Interfaces:**
- Consumes: `database.get_user_identity/get_user/update_user_identity_display_name/get_allowed_identity/has_any_allowed_identity/create_user/create_user_identity/delete_allowed_identity` (Task 3). `identity_providers.get_provider(slug)` (existing).
- Produces: `resolve_login(provider: str, raw_id: str, display_name: str) -> str | None` and `is_session_valid(username: str) -> bool` — both used by Task 5 (`validate_personal_token`) and Task 7/8 (`admin_auth`, `oauth_router`).

This task does NOT yet remove the old `is_allowed`/`is_admin`/`can_manage` — that's Task 5, kept separate so this task's diff is reviewable on its own.

- [ ] **Step 1: Write failing tests**

Add to `packages/aggregator/tests/test_access_control.py` (near the top, alongside the other imports — add `from aggregator import database` if not already present):

```python
async def test_resolve_login_auto_provisions_when_no_allowlist_restriction():
    canonical = await access_control.resolve_login("github", "resolve-new-user", "New User")
    assert canonical is not None
    assert canonical.startswith("user:")
    identity = await database.get_user_identity("github", "resolve-new-user")
    assert identity is not None
    assert identity.display_name == "New User"


async def test_resolve_login_denies_when_allowlist_restricts_and_not_listed():
    row = await database.create_allowed_identity("github", "resolve-someone-else")
    try:
        result = await access_control.resolve_login("github", "resolve-not-listed", "X")
        assert result is None
    finally:
        await database.delete_allowed_identity(row.id)


async def test_resolve_login_provisions_and_consumes_matching_allowlist_row():
    row = await database.create_allowed_identity("github", "resolve-listed-user", grant_admin=True)
    canonical = await access_control.resolve_login("github", "resolve-listed-user", "Listed")
    assert canonical is not None
    user_id = int(canonical.removeprefix("user:"))
    user = await database.get_user(user_id)
    assert user.is_admin is True  # grant_admin carried through
    # Consumed: the allowed_identities row must be gone after use.
    assert await database.get_allowed_identity("github", "resolve-listed-user") is None
    assert await database.get_allowed_identity("github", "nonexistent-row-cleanup") is None
    if await database.get_allowed_identity("github", "resolve-listed-user") is not None:
        await database.delete_allowed_identity(row.id)  # safety net, shouldn't be needed


async def test_resolve_login_returns_existing_canonical_for_known_identity():
    first = await access_control.resolve_login("github", "resolve-repeat-login", "First")
    second = await access_control.resolve_login("github", "resolve-repeat-login", "First Again")
    assert first == second  # same account both times, not a new one


async def test_resolve_login_updates_display_name_on_repeat_login():
    await access_control.resolve_login("steam", "76500000000000010", "Old Name")
    await access_control.resolve_login("steam", "76500000000000010", "Refreshed Name")
    identity = await database.get_user_identity("steam", "76500000000000010")
    assert identity.display_name == "Refreshed Name"


async def test_resolve_login_denies_when_existing_user_not_allowed():
    canonical = await access_control.resolve_login("github", "resolve-to-disable", "X")
    user_id = int(canonical.removeprefix("user:"))
    await database.update_user_flags(user_id, allowed=False)
    result = await access_control.resolve_login("github", "resolve-to-disable", "X")
    assert result is None


async def test_resolve_login_denies_when_provider_unconfigured(monkeypatch):
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "")  # unconfigured
    result = await access_control.resolve_login("steam", "76500000000000099", "X")
    assert result is None


async def test_is_session_valid_true_for_allowed_user_false_for_disabled():
    canonical = await access_control.resolve_login("github", "session-valid-test", "X")
    assert await access_control.is_session_valid(canonical)
    user_id = int(canonical.removeprefix("user:"))
    await database.update_user_flags(user_id, allowed=False)
    assert not await access_control.is_session_valid(canonical)


async def test_is_session_valid_false_for_malformed_username():
    assert not await access_control.is_session_valid("not-a-canonical-id")
    assert not await access_control.is_session_valid("user:not-a-number")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_access_control.py -k "resolve_login or is_session_valid" -v`
Expected: FAIL with `AttributeError: module 'aggregator.access_control' has no attribute 'resolve_login'`.

- [ ] **Step 3: Implement `resolve_login` and `is_session_valid`**

In `packages/aggregator/src/aggregator/access_control.py`, add `from . import database` to the imports if not already present as a plain module import (currently it does `from .database import (...)` — keep that, and ALSO add `from . import database` as a separate import line so the new code can call e.g. `database.get_user_identity` without adding every new helper to the existing explicit import list). Also add `from .models import User` to the `.models` import line (currently `from .models import Server, ServerVisibility`).

Add these functions right after the module docstring, before the existing `is_allowed` function (which Task 5 will remove):

```python
def _parse_user_id(username: str) -> int | None:
    prefix, sep, raw = username.partition(":")
    if prefix != "user" or not sep or not raw.isdigit():
        return None
    return int(raw)


async def _get_user(username: str) -> User | None:
    user_id = _parse_user_id(username)
    if user_id is None:
        return None
    return await database.get_user(user_id)


async def resolve_login(provider: str, raw_id: str, display_name: str) -> str | None:
    """Given a freshly-verified provider identity, return the canonical
    "user:<id>" session identity, or None if this person may not log in.
    Auto-provisions a new User on first login for identities covered by
    the allow-list (or when that provider's allow-list is empty --
    unrestricted, matching this project's pre-account-linking semantics)."""
    provider_impl = identity_providers.get_provider(provider)
    if provider_impl is None or not provider_impl.is_configured():
        return None

    identity = await database.get_user_identity(provider, raw_id)
    if identity is not None:
        user = await database.get_user(identity.user_id)
        if user is None or not user.allowed:
            return None
        await database.update_user_identity_display_name(identity.id, display_name)
        return f"user:{user.id}"

    allowed_row = await database.get_allowed_identity(provider, raw_id)
    provider_is_restricted = await database.has_any_allowed_identity(provider)
    if allowed_row is None and provider_is_restricted:
        return None

    new_user = await database.create_user(
        is_admin=allowed_row.grant_admin if allowed_row else False
    )
    await database.create_user_identity(new_user.id, provider, raw_id, display_name)
    if allowed_row is not None:
        await database.delete_allowed_identity(allowed_row.id)
    return f"user:{new_user.id}"


async def is_session_valid(username: str) -> bool:
    """True if `username` ("user:<id>") refers to a User that still exists
    and is allowed. Used to re-validate a standing session cookie or
    personal token on every request -- see the spec's note on why
    provider-configured-ness is no longer part of this ongoing check
    (docs/superpowers/specs/2026-08-24-account-linking-admin-management-design.md)."""
    user = await _get_user(username)
    return bool(user and user.allowed)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_access_control.py -k "resolve_login or is_session_valid" -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
cd packages/aggregator
git add src/aggregator/access_control.py tests/test_access_control.py
git commit -m "feat(aggregator): add access_control.resolve_login for provider-agnostic login resolution"
```

---

### Task 5: Make access checks async; remove `is_allowed`; update call sites

**Files:**
- Modify: `packages/aggregator/src/aggregator/access_control.py`
- Modify: `packages/aggregator/src/aggregator/api/routers.py`
- Modify: `packages/aggregator/src/aggregator/meta_tools.py`
- Test: `packages/aggregator/tests/test_access_control.py` (rewrite the parts described below)

**Interfaces:**
- Consumes: `_get_user`, `is_session_valid` (Task 4).
- Produces: `is_admin(username) -> bool` (now `async`), `can_manage(server, username) -> bool` (now `async`), `visible_servers(username) -> list[Server]` (already async, internals change), `visible_server_names(username) -> set[str]` (unchanged signature), `validate_personal_token(token) -> str | None` (now checks `is_session_valid` instead of the removed `is_allowed`). `is_allowed` and the `_is_visible` standalone helper are deleted.

- [ ] **Step 1: Replace the old tests that exercised `is_allowed`/sync `is_admin` with new ones**

In `packages/aggregator/tests/test_access_control.py`, **delete** every test from `test_is_admin_true_only_for_admin_users_env` through `test_is_allowed_false_for_unprefixed_username` (i.e. every test that references `access_control.is_allowed`, the module-level `ADMIN`/`OWNER`/`STRANGER` string constants used as raw identities, or calls `access_control.is_admin`/`can_manage`/`visible_servers`/`visible_server_names` without `await`). Also delete the module-level `ADMIN = "github:test-admin"` / `OWNER = "github:ac-owner"` / `STRANGER = "github:ac-stranger"` constants and the `test_generate_and_validate_personal_token_round_trip` / `test_regenerating_token_invalidates_previous_one` / `test_validate_personal_token_unknown_token_returns_none` / `test_validate_personal_token_rejects_deprovisioned_user` tests (these all pass raw prefixed strings directly to `generate_personal_token`, which is no longer how a real personal token comes to exist).

Replace them with:

```python
async def _make_user(raw_id: str, *, is_admin: bool = False, provider: str = "github") -> str:
    """Test helper: create a real User + UserIdentity via the same
    resolve_login() auto-provisioning path a real first login exercises,
    optionally promoting it to admin afterward. Returns the canonical
    "user:<id>" identity."""
    canonical = await access_control.resolve_login(provider, raw_id, raw_id)
    if is_admin:
        user_id = int(canonical.removeprefix("user:"))
        await database.update_user_flags(user_id, is_admin=True)
    return canonical


async def test_is_admin_true_only_for_admin_user():
    admin = await _make_user("is-admin-test-admin", is_admin=True)
    regular = await _make_user("is-admin-test-regular")
    assert await access_control.is_admin(admin)
    assert not await access_control.is_admin(regular)


async def test_is_admin_false_when_account_disabled():
    admin = await _make_user("is-admin-disabled-test", is_admin=True)
    user_id = int(admin.removeprefix("user:"))
    await database.update_user_flags(user_id, allowed=False)
    assert not await access_control.is_admin(admin)


async def test_can_manage_true_for_owner_and_admin_false_for_stranger():
    owner = await _make_user("can-manage-owner")
    admin = await _make_user("can-manage-admin", is_admin=True)
    stranger = await _make_user("can-manage-stranger")
    server = await add_server(
        "ac-can-manage",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=owner,
        visibility=ServerVisibility.PRIVATE.value,
    )
    try:
        assert await access_control.can_manage(server, owner)
        assert await access_control.can_manage(server, admin)
        assert not await access_control.can_manage(server, stranger)
    finally:
        await _cleanup(server.id)


async def test_visible_servers_private_only_to_owner_and_admin():
    owner = await _make_user("visible-private-owner")
    admin = await _make_user("visible-private-admin", is_admin=True)
    stranger = await _make_user("visible-private-stranger")
    server = await add_server(
        "ac-visible-private",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=owner,
        visibility=ServerVisibility.PRIVATE.value,
    )
    try:
        owner_names = {s.name for s in await access_control.visible_servers(owner)}
        admin_names = {s.name for s in await access_control.visible_servers(admin)}
        stranger_names = {s.name for s in await access_control.visible_servers(stranger)}
        assert server.name in owner_names
        assert server.name in admin_names
        assert server.name not in stranger_names
    finally:
        await _cleanup(server.id)


async def test_visible_servers_everyone_visible_to_all():
    owner = await _make_user("visible-everyone-owner")
    stranger = await _make_user("visible-everyone-stranger")
    server = await add_server(
        "ac-visible-everyone",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=owner,
        visibility=ServerVisibility.EVERYONE.value,
    )
    try:
        stranger_names = {s.name for s in await access_control.visible_servers(stranger)}
        assert server.name in stranger_names
    finally:
        await _cleanup(server.id)


async def test_visible_server_names_matches_visible_servers():
    owner = await _make_user("visible-names-owner")
    stranger = await _make_user("visible-names-stranger")
    server = await add_server(
        "ac-visible-names",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=owner,
        visibility=ServerVisibility.EVERYONE.value,
    )
    try:
        names = await access_control.visible_server_names(stranger)
        assert server.name in names
    finally:
        await _cleanup(server.id)


async def test_generate_and_validate_personal_token_round_trip():
    user = await _make_user("token-round-trip-user")
    token = await access_control.generate_personal_token(user)
    assert await access_control.validate_personal_token(token) == user


async def test_regenerating_token_invalidates_previous_one():
    user = await _make_user("token-regen-user")
    old_token = await access_control.generate_personal_token(user)
    new_token = await access_control.generate_personal_token(user)
    assert await access_control.validate_personal_token(old_token) is None
    assert await access_control.validate_personal_token(new_token) == user


async def test_validate_personal_token_unknown_token_returns_none():
    assert await access_control.validate_personal_token("not-a-real-token") is None


async def test_validate_personal_token_rejects_disabled_user():
    """A personal token must stop working once its owner's account is
    disabled -- the token store alone must not keep a revoked user's
    access alive (mirrors admin_auth.get_session_user's same check for the
    session-cookie path)."""
    user = await _make_user("token-disabled-user")
    token = await access_control.generate_personal_token(user)
    user_id = int(user.removeprefix("user:"))
    await database.update_user_flags(user_id, allowed=False)
    assert await access_control.validate_personal_token(token) is None
```

Also update the module docstring at the top of `test_access_control.py` (currently references "the visibility/ownership/personal-token rules") to add one sentence noting these now exercise the DB-backed `User`/`UserIdentity` model via `resolve_login`, not raw env-var-backed prefixed strings.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_access_control.py -v`
Expected: FAIL — `TypeError` (calling `is_admin`/`can_manage` without `await` returns a coroutine, comparisons against it fail) or similar, since the implementation hasn't changed yet.

- [ ] **Step 3: Rewrite `access_control.py`'s access-check functions**

In `packages/aggregator/src/aggregator/access_control.py`:

Remove the `from .config import ADMIN_USERS, GITHUB_ALLOWED_USERS, STEAM_ALLOWED_USERS` import line entirely (no longer read at runtime by this module).

Delete the `is_allowed` function entirely.

Replace `is_admin`, `can_manage`, `_is_visible`, `visible_servers` with:

```python
async def is_admin(username: str) -> bool:
    user = await _get_user(username)
    return bool(user and user.is_admin and user.allowed)


async def can_manage(server: Server, username: str) -> bool:
    if await is_admin(username):
        return True
    return server.owner_username == username


async def visible_servers(username: str) -> list[Server]:
    admin = await is_admin(username)

    def _visible(server: Server) -> bool:
        if admin:
            return True
        if server.visibility == ServerVisibility.EVERYONE.value:
            return True
        return server.owner_username == username

    return [s for s in await list_servers() if _visible(s)]
```

(`visible_server_names` below it is unchanged — it already just calls `visible_servers`.)

Update `validate_personal_token`:

```python
async def validate_personal_token(token: str) -> str | None:
    username = await get_username_by_token_hash(_hash_token(token))
    if username and not await is_session_valid(username):
        return None
    return username
```

Update the module docstring at the top of the file (currently ends with "...or whether a resolved identity (from any identity provider) is allowed to authenticate at all") to instead say "...or whether a resolved provider identity is allowed to log in and become (or reach) a User account — see `resolve_login`."

- [ ] **Step 4: Update call sites in `routers.py` and `meta_tools.py`**

In `packages/aggregator/src/aggregator/api/routers.py`, add `await` before every `access_control.can_manage(...)` call — there are 5: in `api_update_server`, `api_delete_server`, `api_enable_server`, `api_disable_server`, `api_restart_server`. Example (apply the same `if not existing or not access_control.can_manage(existing, username):` → `if not existing or not await access_control.can_manage(existing, username):` pattern at all 5 sites):

```python
    existing = await get_server(server_id)
    if not existing or not await access_control.can_manage(existing, username):
        raise HTTPException(status_code=404, detail="Server not found")
```

In `packages/aggregator/src/aggregator/meta_tools.py`, update `_find_by_name`:

```python
async def _find_by_name(name: str, username: str) -> Server:
    for server in await database.list_servers():
        if server.name == name and await access_control.can_manage(server, username):
            return server
    raise ValueError(f"No server named {name!r}")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_access_control.py -v`
Expected: PASS, all tests in the file. (Task 6+ will fix the rest of the suite — `test_admin_auth.py`, `test_oauth.py`, etc. are expected to still fail after this task; that's normal and addressed in their own tasks.)

- [ ] **Step 6: Commit**

```bash
cd packages/aggregator
git add src/aggregator/access_control.py src/aggregator/api/routers.py src/aggregator/meta_tools.py tests/test_access_control.py
git commit -m "feat(aggregator): make access checks async, DB-backed; remove is_allowed"
```

---

### Task 6: Self-service linking core — `link_identity`, `unlink_identity`

**Files:**
- Modify: `packages/aggregator/src/aggregator/access_control.py`
- Test: `packages/aggregator/tests/test_access_control.py`

**Interfaces:**
- Consumes: `_get_user`, `database.get_user_identity/list_user_identities/create_user_identity/update_user_identity_display_name/delete_user_identity` (Tasks 3-4).
- Produces: `link_identity(current_username, provider, raw_id, display_name) -> str` (returns `"ok"`, `"conflict"`, or `"invalid"`), `unlink_identity(current_username, identity_id) -> str` (returns `"ok"`, `"not_found"`, or `"last_identity"`) — both used by Task 8 (`admin_auth`) and Task 11 (`main.py`'s unlink endpoint).

- [ ] **Step 1: Write failing tests**

Add to `packages/aggregator/tests/test_access_control.py`:

```python
async def test_link_identity_attaches_new_identity_to_current_user():
    user = await _make_user("link-base-user")
    outcome = await access_control.link_identity(user, "steam", "76500000000000020", "Gamer")
    assert outcome == "ok"
    identities = await database.list_user_identities(int(user.removeprefix("user:")))
    assert any(i.provider == "steam" and i.raw_id == "76500000000000020" for i in identities)


async def test_link_identity_idempotent_relink_to_same_user():
    user = await _make_user("link-idempotent-user")
    await access_control.link_identity(user, "steam", "76500000000000021", "Gamer")
    outcome = await access_control.link_identity(user, "steam", "76500000000000021", "Gamer2")
    assert outcome == "ok"
    identities = await database.list_user_identities(int(user.removeprefix("user:")))
    steam_identities = [i for i in identities if i.provider == "steam"]
    assert len(steam_identities) == 1
    assert steam_identities[0].display_name == "Gamer2"  # display name refreshed


async def test_link_identity_conflict_when_owned_by_different_user():
    victim = await _make_user("link-conflict-victim")
    await access_control.link_identity(victim, "steam", "76500000000000022", "Victim's Steam")
    attacker = await _make_user("link-conflict-attacker")
    outcome = await access_control.link_identity(attacker, "steam", "76500000000000022", "X")
    assert outcome == "conflict"
    # Victim's identity must be untouched.
    identity = await database.get_user_identity("steam", "76500000000000022")
    assert identity.user_id == int(victim.removeprefix("user:"))


async def test_link_identity_conflict_when_already_has_this_provider():
    user = await _make_user("link-same-provider-user")  # already has a github identity
    outcome = await access_control.link_identity(user, "github", "link-second-github", "X")
    assert outcome == "conflict"


async def test_link_identity_invalid_for_disabled_account():
    user = await _make_user("link-disabled-account")
    user_id = int(user.removeprefix("user:"))
    await database.update_user_flags(user_id, allowed=False)
    outcome = await access_control.link_identity(user, "steam", "76500000000000023", "X")
    assert outcome == "invalid"


async def test_unlink_identity_removes_non_last_identity():
    user = await _make_user("unlink-base-user")
    await access_control.link_identity(user, "steam", "76500000000000030", "X")
    identities = await database.list_user_identities(int(user.removeprefix("user:")))
    steam_identity = next(i for i in identities if i.provider == "steam")
    outcome = await access_control.unlink_identity(user, steam_identity.id)
    assert outcome == "ok"
    remaining = await database.list_user_identities(int(user.removeprefix("user:")))
    assert all(i.provider != "steam" for i in remaining)


async def test_unlink_identity_refuses_to_remove_last_identity():
    user = await _make_user("unlink-only-identity-user")
    identities = await database.list_user_identities(int(user.removeprefix("user:")))
    outcome = await access_control.unlink_identity(user, identities[0].id)
    assert outcome == "last_identity"
    # Must still be there.
    assert len(await database.list_user_identities(int(user.removeprefix("user:")))) == 1


async def test_unlink_identity_not_found_for_wrong_owner():
    owner = await _make_user("unlink-owner-user")
    other = await _make_user("unlink-other-user")
    await access_control.link_identity(owner, "steam", "76500000000000031", "X")
    identities = await database.list_user_identities(int(owner.removeprefix("user:")))
    steam_identity = next(i for i in identities if i.provider == "steam")
    outcome = await access_control.unlink_identity(other, steam_identity.id)
    assert outcome == "not_found"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_access_control.py -k "link_identity or unlink_identity" -v`
Expected: FAIL with `AttributeError: module 'aggregator.access_control' has no attribute 'link_identity'`.

- [ ] **Step 3: Implement `link_identity` and `unlink_identity`**

In `packages/aggregator/src/aggregator/access_control.py`, add after `resolve_login`:

```python
async def link_identity(current_username: str, provider: str, raw_id: str, display_name: str) -> str:
    """Attach (provider, raw_id) to the account behind `current_username`
    ("user:<id>"). Self-service only -- called after the target provider
    has already verified the identity via its own login flow while the
    caller was already authenticated as current_username (see
    admin_auth.handle_link_callback). Returns "ok" on success (including
    an idempotent re-link of an identity already owned by this same
    account), "conflict" if the identity is already linked to a
    *different* account or this account already has a different identity
    for the same provider, or "invalid" if current_username isn't a real,
    allowed account."""
    user = await _get_user(current_username)
    if user is None or not user.allowed:
        return "invalid"

    existing = await database.get_user_identity(provider, raw_id)
    if existing is not None:
        if existing.user_id != user.id:
            return "conflict"
        await database.update_user_identity_display_name(existing.id, display_name)
        return "ok"

    identities = await database.list_user_identities(user.id)
    if any(i.provider == provider for i in identities):
        return "conflict"

    await database.create_user_identity(user.id, provider, raw_id, display_name)
    return "ok"


async def unlink_identity(current_username: str, identity_id: int) -> str:
    """Remove a linked identity from the account behind `current_username`.
    Returns "ok", "not_found" (the identity doesn't exist or belongs to a
    different account -- both look the same to the caller, deliberately,
    so this can't be used to probe which identities exist), or
    "last_identity" (refused -- would leave the account with zero ways to
    log in)."""
    user = await _get_user(current_username)
    if user is None:
        return "not_found"
    identities = await database.list_user_identities(user.id)
    target = next((i for i in identities if i.id == identity_id), None)
    if target is None:
        return "not_found"
    if len(identities) <= 1:
        return "last_identity"
    await database.delete_user_identity(identity_id)
    return "ok"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_access_control.py -v`
Expected: PASS, entire file.

- [ ] **Step 5: Commit**

```bash
cd packages/aggregator
git add src/aggregator/access_control.py tests/test_access_control.py
git commit -m "feat(aggregator): add access_control.link_identity/unlink_identity"
```

---

### Task 7: `admin_auth.py` — async session checks, `require_admin`, `resolve_login`-based callback

**Files:**
- Modify: `packages/aggregator/src/aggregator/admin_auth.py`
- Test: `packages/aggregator/tests/test_admin_auth.py`

**Interfaces:**
- Consumes: `access_control.is_session_valid`, `access_control.resolve_login`, `access_control.is_admin` (Tasks 4-5).
- Produces: `get_session_user(request) -> str | None` (now `async`), `require_api_auth(request) -> str` (unchanged signature, now awaits), `require_admin(request) -> str` (new), `handle_callback(request, provider) -> RedirectResponse` (now resolves via `resolve_login`, session cookie carries canonical `"user:<id>"`).

- [ ] **Step 1: Update the failing/obsolete tests**

In `packages/aggregator/tests/test_admin_auth.py`, replace `test_get_session_user_returns_username_for_valid_cookie` and `test_get_session_user_returns_none_when_not_allowed` (which monkeypatch the now-removed `access_control.GITHUB_ALLOWED_USERS`) with:

```python
async def test_get_session_user_returns_username_for_valid_cookie():
    from aggregator import access_control

    canonical = await access_control.resolve_login("github", "admin-auth-valid-user", "X")
    cookie = _session_cookie(canonical, "X")
    assert await get_session_user(_request_with_cookie(cookie)) == canonical


async def test_get_session_user_returns_none_when_account_disabled():
    from aggregator import access_control, database

    canonical = await access_control.resolve_login("github", "admin-auth-disabled-user", "X")
    user_id = int(canonical.removeprefix("user:"))
    await database.update_user_flags(user_id, allowed=False)
    cookie = _session_cookie(canonical, "X")
    assert await get_session_user(_request_with_cookie(cookie)) is None
```

Update the other `get_session_user`/`get_session_display_name` tests that don't depend on allow-list monkeypatching (`test_get_session_user_returns_none_for_garbage_cookie`, `test_get_session_user_returns_none_when_no_cookie`, `test_get_session_user_returns_none_for_legacy_plain_string_payload`, `test_get_session_display_name_returns_display_name`, `test_get_session_display_name_returns_none_for_garbage_cookie`) to `async def` and add `await` before each `get_session_user(...)` call (they don't otherwise change — `get_session_display_name` itself stays synchronous, it only decodes the cookie payload, no DB lookup).

For `test_get_session_display_name_returns_display_name`, remove the `monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", set())` line entirely (that attribute no longer exists) — the test doesn't need it since `get_session_display_name` never called `is_allowed` even before.

Replace `test_require_api_auth_accepts_valid_personal_token` (uses a raw `"github:auth-test-user"` string directly):

```python
async def test_require_api_auth_accepts_valid_personal_token():
    from aggregator import access_control

    canonical = await access_control.resolve_login("github", "require-api-auth-user", "X")
    token = await access_control.generate_personal_token(canonical)
    username = await require_api_auth(_request_with_headers({"authorization": f"Bearer {token}"}))
    assert username == canonical
```

Replace `test_handle_callback_sets_session_cookie_on_success` (monkeypatches the removed `access_control.GITHUB_ALLOWED_USERS`):

```python
async def test_handle_callback_sets_session_cookie_on_success():
    provider = _FakeProvider()
    provider.resolve_callback = AsyncMock(
        return_value=ProviderResult(username="github:handle-callback-success", display_name="X")
    )

    login_response = admin_auth.login_redirect(provider)
    state_cookie = login_response.headers["set-cookie"]
    cookie_value = state_cookie.split("admin_oauth_state=")[1].split(";")[0]
    raw_state = admin_auth._state_signer.loads(cookie_value, max_age=admin_auth.STATE_MAX_AGE)

    request = Request(
        {
            "type": "http",
            "query_string": f"state={raw_state}".encode(),
            "headers": [(b"cookie", f"admin_oauth_state={cookie_value}".encode())],
        }
    )
    response = await admin_auth.handle_callback(request, provider)
    assert response.status_code == 302
    assert response.headers["location"] == "/admin"
    assert "admin_session=" in response.headers.get("set-cookie", "")
```

(`test_handle_callback_rejects_state_mismatch` and `test_handle_callback_rejects_when_provider_returns_none` need no change — neither depends on allow-list state.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_admin_auth.py -v`
Expected: FAIL — the not-yet-`await`ed `get_session_user` calls raise `TypeError` when compared/used as a bool, and `handle_callback` still calls the now-deleted `access_control.is_allowed`.

- [ ] **Step 3: Rewrite `admin_auth.py`**

In `packages/aggregator/src/aggregator/admin_auth.py`, change `get_session_user`:

```python
async def get_session_user(request: Request) -> str | None:
    """Return the authenticated (canonical "user:<id>") username from the
    session cookie, or None."""
    payload = _load_session_payload(request)
    if payload is None:
        return None
    username = payload.get("username")
    if not username or not await access_control.is_session_valid(username):
        return None
    return username
```

Change `require_api_auth`:

```python
async def require_api_auth(request: Request) -> str:
    """FastAPI dependency for /api/* routes.

    Accepts an admin session cookie (browser) or a personal-token Bearer
    header (programmatic access). Does not accept MCP OAuth tokens — those
    are only valid for /mcp and /messages. Returns the authenticated
    username so callers can scope their query to it.
    """
    user = await get_session_user(request)
    if user:
        return user
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        username = await access_control.validate_personal_token(auth[7:])
        if username:
            return username
    raise HTTPException(status_code=401, detail="Unauthorized")


async def require_admin(request: Request) -> str:
    """FastAPI dependency for admin-only /api/* routes (see
    api/users_router.py). Same acceptance as require_api_auth, plus an
    admin-rights check."""
    user = await require_api_auth(request)
    if not await access_control.is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
```

Change `handle_callback`:

```python
async def handle_callback(request: Request, provider: IdentityProvider) -> RedirectResponse:
    """Handle an identity provider's callback for the admin browser flow,
    set session cookie, redirect to /admin."""
    state_token = request.cookies.get("admin_oauth_state")
    if not state_token:
        return _login_error("Missing state cookie — possible CSRF")
    try:
        stored_state = _state_signer.loads(state_token, max_age=STATE_MAX_AGE)
    except BadSignature, SignatureExpired:
        return _login_error("Invalid or expired state — please try again")
    request_state = request.query_params.get("state")
    if not request_state or not secrets.compare_digest(request_state, stored_state):
        return _login_error("State mismatch — please try again")

    result = await provider.resolve_callback(request)
    if result is None:
        return _login_error("Authentication error — please try again")

    provider_slug, _, raw_id = result.username.partition(":")
    canonical = await access_control.resolve_login(provider_slug, raw_id, result.display_name)
    if canonical is None:
        logger.warning("Admin login denied: %s not allowed", result.username)
        return _login_error(f"User '{result.username}' is not authorized")

    logger.info("Admin login: %s (%s)", canonical, result.username)
    session_value = _signer.dumps({"username": canonical, "display_name": result.display_name})
    response = RedirectResponse("/admin", status_code=302)
    response.set_cookie(
        "admin_session",
        session_value,
        httponly=True,
        max_age=SESSION_MAX_AGE,
        secure=True,
        samesite="lax",
    )
    response.delete_cookie("admin_oauth_state")
    return response
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_admin_auth.py -v`
Expected: PASS, entire file.

- [ ] **Step 5: Commit**

```bash
cd packages/aggregator
git add src/aggregator/admin_auth.py tests/test_admin_auth.py
git commit -m "feat(aggregator): admin_auth uses resolve_login, adds require_admin, async session checks"
```

---

### Task 8: Self-service linking flow — `login_redirect_for_link`, `handle_link_callback`

**Files:**
- Modify: `packages/aggregator/src/aggregator/admin_auth.py`
- Test: `packages/aggregator/tests/test_admin_auth.py`

**Interfaces:**
- Consumes: `get_session_user` (Task 7), `access_control.link_identity` (Task 6).
- Produces: `login_redirect_for_link(request, provider) -> RedirectResponse` (raises `HTTPException(401)` if not authenticated), `handle_link_callback(request, provider) -> RedirectResponse` — both used by Task 9's `_handle_oauth_callback` third branch and Task 11's `/admin/link/{provider}` route.

- [ ] **Step 1: Write failing tests**

Add to `packages/aggregator/tests/test_admin_auth.py`:

```python
async def test_login_redirect_for_link_requires_authenticated_session():
    provider = _FakeProvider()
    request = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as exc_info:
        await admin_auth.login_redirect_for_link(request, provider)
    assert exc_info.value.status_code == 401


async def test_login_redirect_for_link_sets_state_cookie_when_authenticated():
    from aggregator import access_control

    canonical = await access_control.resolve_login("github", "link-redirect-user", "X")
    cookie = _session_cookie(canonical, "X")
    request = Request(
        {"type": "http", "headers": [(b"cookie", f"admin_session={cookie}".encode())]}
    )
    provider = _FakeProvider()
    response = await admin_auth.login_redirect_for_link(request, provider)
    assert response.status_code == 302
    assert "fake-provider.example" in response.headers["location"]
    assert "link_identity_state=" in response.headers.get("set-cookie", "")


async def test_handle_link_callback_attaches_identity_to_current_user():
    from aggregator import access_control, database

    canonical = await access_control.resolve_login("github", "link-callback-user", "X")
    cookie = _session_cookie(canonical, "X")
    login_request = Request(
        {"type": "http", "headers": [(b"cookie", f"admin_session={cookie}".encode())]}
    )
    provider = _FakeProvider()
    login_response = await admin_auth.login_redirect_for_link(login_request, provider)
    state_cookie = login_response.headers["set-cookie"]
    cookie_value = state_cookie.split("link_identity_state=")[1].split(";")[0]
    stored = admin_auth._link_state_signer.loads(cookie_value, max_age=admin_auth.STATE_MAX_AGE)

    provider.resolve_callback = AsyncMock(
        return_value=ProviderResult(username="steam:76500000000000040", display_name="Gamer")
    )
    callback_request = Request(
        {
            "type": "http",
            "query_string": f"state={stored['state']}".encode(),
            "headers": [(b"cookie", f"link_identity_state={cookie_value}".encode())],
        }
    )
    response = await admin_auth.handle_link_callback(callback_request, provider)
    assert response.status_code == 302

    identities = await database.list_user_identities(int(canonical.removeprefix("user:")))
    assert any(i.provider == "steam" and i.raw_id == "76500000000000040" for i in identities)


async def test_handle_link_callback_rejects_state_mismatch():
    provider = _FakeProvider()
    request = Request(
        {
            "type": "http",
            "query_string": b"state=wrong-state",
            "headers": [
                (
                    b"cookie",
                    b"link_identity_state="
                    + admin_auth._link_state_signer.dumps(
                        {"state": "real-state", "user": "user:1"}
                    ).encode(),
                )
            ],
        }
    )
    response = await admin_auth.handle_link_callback(request, provider)
    assert response.status_code == 302
    assert "link_error=" in response.headers["location"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_admin_auth.py -k "login_redirect_for_link or handle_link_callback" -v`
Expected: FAIL with `AttributeError: module 'aggregator.admin_auth' has no attribute 'login_redirect_for_link'`.

- [ ] **Step 3: Implement the linking flow**

In `packages/aggregator/src/aggregator/admin_auth.py`, add `import urllib.parse` is already imported — good. Add a new signer near the top (alongside `_signer`/`_state_signer`):

```python
_link_state_signer = URLSafeTimedSerializer(SESSION_SECRET, salt="admin-link-state")
```

Add these functions after `handle_callback`:

```python
async def login_redirect_for_link(request: Request, provider: IdentityProvider) -> RedirectResponse:
    """Start `provider`'s login flow to link a new identity onto the
    CURRENTLY authenticated session's account. Raises 401 if there's no
    valid session -- this route must never accept a forged/anonymous
    "link to user X" request. The account to link onto is read from the
    server-signed session cookie, then re-signed into the state cookie
    below -- it can't be supplied or tampered with by the client."""
    current_user = await get_session_user(request)
    if current_user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    state = secrets.token_urlsafe(32)
    state_token = _link_state_signer.dumps({"state": state, "user": current_user})
    response = provider.login_redirect(state)
    response.set_cookie(
        "link_identity_state",
        state_token,
        httponly=True,
        max_age=STATE_MAX_AGE,
        samesite="lax",
    )
    return response


def _link_error(msg: str) -> RedirectResponse:
    response = RedirectResponse(
        f"/admin/account?link_error={urllib.parse.quote(msg)}", status_code=302
    )
    response.delete_cookie("link_identity_state")
    return response


async def handle_link_callback(request: Request, provider: IdentityProvider) -> RedirectResponse:
    """Handle an identity provider's callback for the self-service account
    linking flow, then redirect back to the Account page."""
    state_token = request.cookies.get("link_identity_state")
    if not state_token:
        return _link_error("Missing state cookie — possible CSRF")
    try:
        stored = _link_state_signer.loads(state_token, max_age=STATE_MAX_AGE)
    except BadSignature, SignatureExpired:
        return _link_error("Invalid or expired state — please try again")
    request_state = request.query_params.get("state")
    if not request_state or not secrets.compare_digest(request_state, stored["state"]):
        return _link_error("State mismatch — please try again")

    result = await provider.resolve_callback(request)
    if result is None:
        return _link_error("Authentication error — please try again")

    provider_slug, _, raw_id = result.username.partition(":")
    outcome = await access_control.link_identity(
        stored["user"], provider_slug, raw_id, result.display_name
    )
    if outcome != "ok":
        return _link_error(
            "That account is already linked to a different user"
            if outcome == "conflict"
            else "Your session is no longer valid — please log in again"
        )
    response = RedirectResponse("/admin/account", status_code=302)
    response.delete_cookie("link_identity_state")
    return response
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_admin_auth.py -v`
Expected: PASS, entire file.

- [ ] **Step 5: Commit**

```bash
cd packages/aggregator
git add src/aggregator/admin_auth.py tests/test_admin_auth.py
git commit -m "feat(aggregator): add self-service account-linking flow to admin_auth"
```

---

### Task 9: `oauth.py` — simplify `finish_session` (caller now pre-validates)

**Files:**
- Modify: `packages/aggregator/src/aggregator/oauth.py`
- Test: `packages/aggregator/tests/test_oauth.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `finish_session(oauth_state, username) -> tuple[str, str, str] | None` — same signature, but now assumes `username` is already-resolved AND already-allowed (the caller, Task 10's `oauth_router.py`, now calls `access_control.resolve_login` first).

- [ ] **Step 1: Update the tests**

In `packages/aggregator/tests/test_oauth.py`, remove every `monkeypatch.setattr("aggregator.oauth.access_control.is_allowed", ...)` line (4 occurrences) — `finish_session` no longer consults `access_control` at all. Delete `test_finish_session_rejects_disallowed_user` entirely (that rejection now happens one layer up, in `oauth_router.py`'s caller, covered by Task 10's tests — `finish_session` itself has no more "disallowed" case to test). The remaining tests (`test_finish_session_issues_auth_code_for_allowed_user`, `test_finish_session_rejects_unknown_state`, `test_exchange_code_and_validate_bearer_round_trip`, `test_exchange_code_rejects_pkce_mismatch`, `test_validate_bearer_returns_none_for_unknown_token`) keep their bodies as-is, just with the `monkeypatch.setattr(...)` line removed from each (and the `monkeypatch` parameter dropped from their signatures if it's now unused).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_oauth.py -k test_finish_session_issues_auth_code_for_allowed_user -v`
Expected: FAIL with `AttributeError: <module 'aggregator.access_control'> does not have the attribute 'is_allowed'` — Task 5 already deleted `is_allowed` from `access_control.py`, so `oauth.py`'s still-unmodified `finish_session` (which calls it internally) now breaks the moment a test reaches that line without a monkeypatch already installed for the old, now-nonexistent attribute. This confirms the real starting problem this task fixes.

Run: `cd packages/aggregator && uv run pytest tests/test_oauth_router.py -v`
Expected: Several FAILs — `oauth_router.py` hasn't been updated yet (that's Task 10), so it still drives the old flow through the now-broken `finish_session`. This confirms the suite is mid-migration as expected; Task 10 fixes it. Proceed to Step 3.

- [ ] **Step 3: Simplify `finish_session`**

In `packages/aggregator/src/aggregator/oauth.py`, remove the `from . import access_control` import (no longer used anywhere in this file). Change `finish_session`:

```python
async def finish_session(oauth_state: str, username: str) -> tuple[str, str, str] | None:
    """
    Given an already-resolved AND already-allowed canonical identity
    (access_control.resolve_login has already run in the caller -- see
    api/oauth_router.py), issue an internal auth code. Returns (auth_code,
    client_redirect_uri, client_state), or None if the pending PKCE
    session is unknown or expired.
    """
    session = _sessions.pop(oauth_state, None)
    if not session or session.expires_at < time.time():
        logger.warning("OAuth: unknown or expired state %s", oauth_state[:8])
        return None

    _gc()
    code = secrets.token_urlsafe(32)
    _codes[code] = _AuthCode(
        client_id=session.client_id,
        redirect_uri=session.redirect_uri,
        code_challenge=session.code_challenge,
        username=username,
        expires_at=time.time() + AUTH_CODE_TTL,
    )
    logger.info("OAuth: auth code issued for %s (client=%s)", username, session.client_id)
    return code, session.redirect_uri, session.client_state
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_oauth.py -v`
Expected: PASS, entire file. (`test_oauth_router.py` is still expected to fail — Task 10 fixes it.)

- [ ] **Step 5: Commit**

```bash
cd packages/aggregator
git add src/aggregator/oauth.py tests/test_oauth.py
git commit -m "refactor(aggregator): oauth.finish_session no longer re-checks allow-list"
```

---

### Task 10: `api/oauth_router.py` — canonical resolution + link-flow discriminator

**Files:**
- Modify: `packages/aggregator/src/aggregator/api/oauth_router.py`
- Test: `packages/aggregator/tests/test_oauth_router.py`

**Interfaces:**
- Consumes: `access_control.resolve_login` (Task 4), `admin_auth.handle_link_callback` (Task 8).
- Produces: `_handle_oauth_callback` now checks for `link_identity_state` before `admin_oauth_state`, and computes a canonical username via `resolve_login` before calling `oauth.finish_session`.

- [ ] **Step 1: Update the tests**

In `packages/aggregator/tests/test_oauth_router.py`, add `access_control` to the existing `from aggregator import identity_providers, oauth` import line (becomes `from aggregator import access_control, identity_providers, oauth`).

Replace `test_oauth_callback_github_mcp_flow_issues_redirect_with_code` and `test_oauth_callback_steam_mcp_flow_issues_redirect_with_code` to also mock `resolve_login` (since the callback now calls it between `resolve_callback` and `finish_session`):

```python
async def test_oauth_callback_github_mcp_flow_issues_redirect_with_code(client, monkeypatch):
    monkeypatch.setattr(
        identity_providers.github_provider, "resolve_callback",
        AsyncMock(return_value=ProviderResult(username="github:octocat", display_name="octocat")),
    )
    monkeypatch.setattr(
        access_control, "resolve_login", AsyncMock(return_value="user:1")
    )
    monkeypatch.setattr(oauth, "finish_session", AsyncMock(return_value=("auth-code", "https://client.example/cb", "client-state")))

    resp = await client.get("/oauth/callback", params={"code": "abc", "state": "xyz"})
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://client.example/cb?")
    assert "code=auth-code" in resp.headers["location"]


async def test_oauth_callback_steam_mcp_flow_issues_redirect_with_code(client, monkeypatch):
    monkeypatch.setattr(
        identity_providers.steam_provider, "resolve_callback",
        AsyncMock(return_value=ProviderResult(username="steam:765", display_name="Gamer")),
    )
    monkeypatch.setattr(
        access_control, "resolve_login", AsyncMock(return_value="user:2")
    )
    monkeypatch.setattr(oauth, "finish_session", AsyncMock(return_value=("auth-code", "https://client.example/cb", "client-state")))

    resp = await client.get("/oauth/callback/steam", params={"state": "xyz"})
    assert resp.status_code == 302
    assert "code=auth-code" in resp.headers["location"]
```

Add a new test for the "resolve_login denies" path (this replaces the old `oauth.finish_session`-rejects-disallowed-user coverage, now one layer up):

```python
async def test_oauth_callback_returns_403_when_resolve_login_denies(client, monkeypatch):
    monkeypatch.setattr(
        identity_providers.github_provider, "resolve_callback",
        AsyncMock(return_value=ProviderResult(username="github:not-allowed", display_name="X")),
    )
    monkeypatch.setattr(access_control, "resolve_login", AsyncMock(return_value=None))

    resp = await client.get("/oauth/callback", params={"code": "abc", "state": "xyz"})
    assert resp.status_code == 403
```

Add a test for the new link-flow discriminator branch:

```python
async def test_oauth_callback_link_flow_delegates_to_admin_auth(client, monkeypatch):
    called = {}

    async def fake_handle_link_callback(request, provider):
        called["provider"] = provider.slug
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/admin/account", status_code=302)

    monkeypatch.setattr(
        "aggregator.api.oauth_router.admin_auth.handle_link_callback", fake_handle_link_callback
    )

    resp = await client.get(
        "/oauth/callback",
        params={"code": "abc", "state": "xyz"},
        cookies={"link_identity_state": "present"},
    )
    assert resp.status_code == 302
    assert called["provider"] == "github"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_oauth_router.py -v`
Expected: FAIL — `_handle_oauth_callback` doesn't yet call `resolve_login` or check for `link_identity_state`.

- [ ] **Step 3: Update `_handle_oauth_callback`**

In `packages/aggregator/src/aggregator/api/oauth_router.py`, change the import line to `from .. import access_control, admin_auth, identity_providers, oauth`. Change `_handle_oauth_callback`:

```python
async def _handle_oauth_callback(request: Request, provider: identity_providers.IdentityProvider):
    if request.cookies.get("link_identity_state"):
        return await admin_auth.handle_link_callback(request, provider)
    if request.cookies.get("admin_oauth_state"):
        return await admin_auth.handle_callback(request, provider)

    state = request.query_params.get("state")
    if not state:
        return HTMLResponse("<h1>OAuth error</h1><p>missing_state</p>", status_code=400)

    result = await provider.resolve_callback(request)
    if result is None:
        return HTMLResponse(
            "<h1>Access denied</h1><p>Authentication failed or user is not authorized.</p>",
            status_code=403,
        )

    provider_slug, _, raw_id = result.username.partition(":")
    canonical = await access_control.resolve_login(provider_slug, raw_id, result.display_name)
    if canonical is None:
        return HTMLResponse(
            "<h1>Access denied</h1><p>Authentication failed or user is not authorized.</p>",
            status_code=403,
        )

    finish_result = await oauth.finish_session(state, canonical)
    if not finish_result:
        return HTMLResponse(
            "<h1>Access denied</h1><p>Authentication failed or user is not authorized.</p>",
            status_code=403,
        )

    auth_code, redirect_uri, client_state = finish_result
    qs = urllib.parse.urlencode({"code": auth_code, "state": client_state})
    return RedirectResponse(f"{redirect_uri}?{qs}", status_code=302)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_oauth_router.py -v`
Expected: PASS, entire file.

- [ ] **Step 5: Commit**

```bash
cd packages/aggregator
git add src/aggregator/api/oauth_router.py tests/test_oauth_router.py
git commit -m "feat(aggregator): oauth_router resolves canonical identity via resolve_login, adds link-flow branch"
```

---

### Task 11: `main.py` wiring — link route, unlink endpoint, `/api/me` identities, users_router

**Files:**
- Modify: `packages/aggregator/src/aggregator/main.py`
- Create: `packages/aggregator/src/aggregator/api/users_router.py`
- Test: `packages/aggregator/tests/test_me_endpoints.py`
- Test: `packages/aggregator/tests/test_users_router.py` (new)

**Interfaces:**
- Consumes: `admin_auth.login_redirect_for_link/require_admin` (Tasks 7-8), `access_control.unlink_identity` (Task 6), `database.list_user_identities/list_users/update_user_flags/list_allowed_identities/create_allowed_identity/delete_allowed_identity` (Task 3).
- Produces: `GET /admin/link/{provider}`, `DELETE /api/me/identities/{identity_id}`, `/api/me` response gains `identities`, `GET/PATCH /api/users`, `GET/POST/DELETE /api/allowed-identities`.

This task is large enough to split its test file work into two files (`test_me_endpoints.py` for the `main.py`-level routes, a new `test_users_router.py` for the admin router) so each stays focused.

- [ ] **Step 1: Write failing tests for `main.py`'s new/changed routes**

In `packages/aggregator/tests/test_me_endpoints.py`, replace the module-level `_session_cookie` helper's callers to use real canonical identities instead of the raw `ADMIN`/`USER` string constants. Replace the top of the file's constants and the tests that reference them:

```python
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
    from aggregator import access_control

    user = await _make_user("me-token-bearer-user")
    token = await access_control.generate_personal_token(user)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/api/me/token", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
```

(`test_auth_providers_reflects_configured_state` is unchanged — leave it as-is. Delete the now-unused `ADMIN`/`USER` module constants and the `access_control` import if it becomes unused at module scope — check first, since `_make_user` above imports it locally.)

Add new tests for the link/unlink routes:

```python
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
```

Note `_session_cookie`'s signature in this file currently defaults `display_name` to the username if not given — keep using it as `_session_cookie(user)` (matches the existing helper, no change needed there since it already takes an arbitrary string).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_me_endpoints.py -v`
Expected: FAIL — `/admin/link/{provider}` and `/api/me/identities/{id}` don't exist yet (404s where 401/302/400/200 are expected), and `/api/me`'s response has no `identities` key yet.

- [ ] **Step 3: Wire the routes into `main.py`**

In `packages/aggregator/src/aggregator/main.py`:

Change the `.database` import line to `from .database import init_db, list_servers, list_user_identities`.

Add the new router import alongside the existing ones: `from .api.users_router import router as users_router`.

Register it near the other `app.include_router` calls:

```python
app.include_router(oauth_router)
app.include_router(api_router, prefix="/api")
app.include_router(users_router, prefix="/api")
```

Add a new route after `admin_login_steam`:

```python
@app.get("/admin/link/{provider}")
async def admin_link(provider: str, request: Request):
    p = identity_providers.get_provider(provider)
    if p is None or not p.is_configured():
        raise HTTPException(status_code=400, detail="Unknown or unconfigured provider")
    return await admin_auth.login_redirect_for_link(request, p)
```

Add a new route near `/api/me`:

```python
@app.delete("/api/me/identities/{identity_id}")
async def api_unlink_identity(identity_id: int, request: Request):
    user = await admin_auth.get_session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    outcome = await access_control.unlink_identity(user, identity_id)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Identity not found")
    if outcome == "last_identity":
        raise HTTPException(status_code=400, detail="Cannot remove your last remaining identity")
    return {"deleted": identity_id}
```

Update `/api/me` and `/api/me/token` (both already exist — change them in place):

```python
@app.get("/api/me")
async def api_me(request: Request):
    user = await admin_auth.get_session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    identities = await list_user_identities(int(user.removeprefix("user:")))
    return {
        "username": user,
        "is_admin": await access_control.is_admin(user),
        "display_name": admin_auth.get_session_display_name(request),
        "identities": [
            {"id": i.id, "provider": i.provider, "raw_id": i.raw_id, "display_name": i.display_name}
            for i in identities
        ],
    }


@app.post("/api/me/token")
async def api_generate_token(request: Request):
    user = await admin_auth.get_session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = await access_control.generate_personal_token(user)
    return {"token": token}
```

- [ ] **Step 4: Write `api/users_router.py`**

Create `packages/aggregator/src/aggregator/api/users_router.py`:

```python
"""
Admin-only user management API: list/toggle User accounts, and manage the
AllowedIdentity pre-approval list (the DB-backed replacement for
GITHUB_ALLOWED_USERS/STEAM_ALLOWED_USERS/ADMIN_USERS -- see
docs/superpowers/specs/2026-08-24-account-linking-admin-management-design.md).
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import database, identity_providers
from ..admin_auth import require_admin
from ..models import AllowedIdentity, User, UserIdentity

router = APIRouter(dependencies=[Depends(require_admin)])


def _identity_out(identity: UserIdentity) -> dict:
    return {
        "id": identity.id,
        "provider": identity.provider,
        "raw_id": identity.raw_id,
        "display_name": identity.display_name,
    }


async def _user_out(user: User) -> dict:
    identities = await database.list_user_identities(user.id)
    return {
        "id": user.id,
        "is_admin": user.is_admin,
        "allowed": user.allowed,
        "created_at": user.created_at,
        "identities": [_identity_out(i) for i in identities],
    }


@router.get("/users")
async def api_list_users():
    return [await _user_out(u) for u in await database.list_users()]


class UpdateUserRequest(BaseModel):
    is_admin: bool | None = None
    allowed: bool | None = None


@router.patch("/users/{user_id}")
async def api_update_user(
    user_id: int, req: UpdateUserRequest, current: str = Depends(require_admin)
):
    current_id = int(current.removeprefix("user:"))
    if user_id == current_id:
        if req.is_admin is False:
            raise HTTPException(status_code=400, detail="Cannot remove your own admin rights")
        if req.allowed is False:
            raise HTTPException(status_code=400, detail="Cannot disable your own account")
    updated = await database.update_user_flags(user_id, is_admin=req.is_admin, allowed=req.allowed)
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")
    return await _user_out(updated)


class AllowedIdentityRequest(BaseModel):
    provider: str
    raw_id: str
    grant_admin: bool = False


def _allowed_out(row: AllowedIdentity) -> dict:
    return {"id": row.id, "provider": row.provider, "raw_id": row.raw_id, "grant_admin": row.grant_admin}


@router.get("/allowed-identities")
async def api_list_allowed_identities():
    return [_allowed_out(r) for r in await database.list_allowed_identities()]


@router.post("/allowed-identities", status_code=201)
async def api_add_allowed_identity(req: AllowedIdentityRequest):
    if req.provider not in identity_providers.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider {req.provider!r}")
    existing = await database.get_allowed_identity(req.provider, req.raw_id)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Already on the allow-list")
    row = await database.create_allowed_identity(req.provider, req.raw_id, req.grant_admin)
    return _allowed_out(row)


@router.delete("/allowed-identities/{allowed_id}")
async def api_delete_allowed_identity(allowed_id: int):
    await database.delete_allowed_identity(allowed_id)
    return {"deleted": allowed_id}
```

- [ ] **Step 5: Write failing tests for `users_router.py`, then run to confirm they fail, then confirm the whole file passes**

Create `packages/aggregator/tests/test_users_router.py`:

```python
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
```

Run: `cd packages/aggregator && uv run pytest tests/test_users_router.py -v`
Expected (before Step 3/4 above): FAIL — routes don't exist. After Step 3/4: PASS, entire file.

- [ ] **Step 6: Run the full main.py-adjacent test suite**

Run: `cd packages/aggregator && uv run pytest tests/test_me_endpoints.py tests/test_users_router.py -v`
Expected: PASS, both files.

- [ ] **Step 7: Commit**

```bash
cd packages/aggregator
git add src/aggregator/main.py src/aggregator/api/users_router.py tests/test_me_endpoints.py tests/test_users_router.py
git commit -m "feat(aggregator): wire self-service linking routes and admin users/allowed-identities API"
```

---

### Task 12: Test-suite sweep — `test_routers.py`, `test_meta_tools.py`, `test_mcp_access_integration.py`, `conftest.py`

**Files:**
- Modify: `packages/aggregator/tests/conftest.py`
- Modify: `packages/aggregator/tests/test_routers.py`
- Modify: `packages/aggregator/tests/test_meta_tools.py`
- Modify: `packages/aggregator/tests/test_mcp_access_integration.py`

**Interfaces:**
- Consumes: `access_control.resolve_login` (Task 4).
- Produces: a `make_user` fixture in `conftest.py`, usable by any test file; every remaining reference to a hardcoded raw `"github:x"`-shaped identity string in these three files replaced with a real account obtained through it.

This task is a **mechanical, rule-driven sweep** across three large, pre-existing test files (585 + 385 + 146 lines) that predate this feature and use the old "raw prefixed string IS the identity" model throughout. Rather than enumerate every individual line here (which would make this task's own text longer than the files it edits), this task specifies the exact transformation rule plus fully-worked examples for each file's dominant pattern. `token_for` itself needs no change — `generate_personal_token`/`PersonalToken.username` always accepted an arbitrary opaque string, and a canonical `"user:<id>"` from `make_user` is exactly that.

- [ ] **Step 1: Add the `make_user` fixture to `conftest.py`**

Add to `packages/aggregator/tests/conftest.py`, after the existing `token_for` fixture:

```python
@pytest.fixture
async def make_user():
    """Factory fixture: `owner = await make_user("router-owner")` (or
    `admin = await make_user("test-admin", is_admin=True)`) creates a real
    User + UserIdentity by driving access_control.resolve_login's
    auto-provisioning path -- the same code a real first login exercises
    -- and returns the canonical "user:<id>" identity. Optionally promotes
    to admin afterward via a direct database call (resolve_login's own
    grant_admin path is reachable only via an AllowedIdentity row, more
    indirection than most tests need)."""
    from aggregator import access_control, database

    async def _make(raw_id: str, *, is_admin: bool = False, provider: str = "github") -> str:
        canonical = await access_control.resolve_login(provider, raw_id, raw_id)
        if is_admin:
            user_id = int(canonical.removeprefix("user:"))
            await database.update_user_flags(user_id, is_admin=True)
        return canonical

    return _make
```

- [ ] **Step 2: Run the three target files to see the current failures**

Run: `cd packages/aggregator && uv run pytest tests/test_routers.py tests/test_meta_tools.py tests/test_mcp_access_integration.py -v 2>&1 | tail -80`
Expected: many FAILs — these files still pass raw prefixed strings as identities directly to endpoints/functions that now require a real `User` (e.g. `can_manage`/`is_admin` return `False` for a string that isn't `"user:<id>"`-shaped, so every "owner can manage their own server" assertion now fails).

- [ ] **Step 3: Apply the transformation rule to `test_routers.py`**

Current pattern at the top of the file:

```python
OWNER = "github:router-owner"
STRANGER = "github:router-stranger"
ADMIN = "github:test-admin"  # set as ADMIN_USERS by conftest.py


@pytest.fixture
async def auth_headers(token_for):
    token = await token_for(OWNER)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def stranger_headers(token_for):
    token = await token_for(STRANGER)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def admin_headers(token_for):
    token = await token_for(ADMIN)
    return {"Authorization": f"Bearer {token}"}
```

Replace with:

```python
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
```

Then, **for every test function in the file that references `OWNER`, `STRANGER`, or `ADMIN` directly** (not just through the header fixtures) — e.g. asserting `server.owner_username == OWNER`, or passing `owner_username=OWNER` directly to `add_server(...)` in test setup — add the corresponding fixture (`owner`, `stranger`, or `admin`) as a parameter to that test function, and replace the constant reference with the fixture parameter. Worked example (the exact shape of every occurrence — grep confirms this is the dominant pattern):

```python
# Before:
async def test_patch_updates_only_provided_fields(client, auth_headers):
    async with ...:
        added = await client.post("/servers", json={...}, headers=auth_headers)
        ...
        assert resp.json()["owner"] == OWNER

# After:
async def test_patch_updates_only_provided_fields(client, auth_headers, owner):
    async with ...:
        added = await client.post("/servers", json={...}, headers=auth_headers)
        ...
        assert resp.json()["owner"] == owner
```

A second worked example for tests that construct a server directly via `database.add_server(..., owner_username=OWNER, ...)` in setup (bypassing the API):

```python
# Before:
server = await add_server("some-name", ServerType.PROXY, "http://x.invalid/mcp",
                           owner_username=OWNER, visibility=ServerVisibility.PRIVATE.value)

# After (add `owner` to the test function's parameters):
server = await add_server("some-name", ServerType.PROXY, "http://x.invalid/mcp",
                           owner_username=owner, visibility=ServerVisibility.PRIVATE.value)
```

After editing, confirm no reference to the old constants remains: `grep -n "\bOWNER\b\|\bSTRANGER\b\|\bADMIN\b" tests/test_routers.py` should return nothing (the fixture names `owner`/`stranger`/`admin` are lowercase, so this grep for the uppercase constants is a clean signal).

- [ ] **Step 4: Run `test_routers.py` to verify it passes**

Run: `cd packages/aggregator && uv run pytest tests/test_routers.py -v`
Expected: PASS, entire file.

- [ ] **Step 5: Apply the same transformation rule to `test_meta_tools.py`**

Run `grep -n "^ADMIN\|^OWNER\|^STRANGER\|= \"github:" tests/test_meta_tools.py` first to find this file's exact constant names (they may differ from `test_routers.py`'s — check before assuming `OWNER`/`STRANGER`/`ADMIN`). Apply the identical rule from Step 3: turn each hardcoded raw-identity constant into a `make_user`-backed fixture, thread it into every test function that references the constant (directly or via a header-building fixture), and confirm via grep that no reference to the old constant names remains.

Run: `cd packages/aggregator && uv run pytest tests/test_meta_tools.py -v`
Expected: PASS, entire file, after the sweep.

- [ ] **Step 6: Apply the same transformation rule to `test_mcp_access_integration.py`**

Run `grep -n "owner_username\|github:" tests/test_mcp_access_integration.py` first — this file boots a real uvicorn server (per its own docstring/name) and is smaller (146 lines), so check whether it uses the same constant pattern or something else (e.g. it may construct identities inline per test rather than via module-level constants — read the matches before editing). Apply the same rule: any raw `"github:x"`-shaped string used as an identity for ownership/admin checks becomes a `make_user`-backed value instead.

Run: `cd packages/aggregator && uv run pytest tests/test_mcp_access_integration.py -v`
Expected: PASS, entire file, after the sweep.

- [ ] **Step 7: Run the complete backend test suite**

Run: `cd packages/aggregator && uv run pytest -q`
Expected: 100% pass, no skips, no errors — this is the first point since Task 5 where the *entire* suite (not just the files each task touched) is green again.

Run: `cd packages/aggregator && uvx ruff check src tests && uvx ruff format --check src tests`
Expected: both clean.

- [ ] **Step 8: Commit**

```bash
cd packages/aggregator
git add tests/conftest.py tests/test_routers.py tests/test_meta_tools.py tests/test_mcp_access_integration.py
git commit -m "test(aggregator): sweep remaining tests to real User accounts via make_user fixture"
```

---

### Task 13: Webui — types, API client, hooks

**Files:**
- Modify: `packages/webui/src/lib/types.ts`
- Modify: `packages/webui/src/lib/api.ts`
- Create: `packages/webui/src/hooks/useUsers.ts`
- Modify: `packages/webui/src/hooks/useMe.ts`

**Interfaces:**
- Produces: `Me.identities`, `UserIdentitySummary`, `User`, `UpdateUserInput`, `AllowedIdentity`, `AddAllowedIdentityInput` types; `api.listUsers/updateUser/listAllowedIdentities/addAllowedIdentity/deleteAllowedIdentity/unlinkIdentity`; `useUsers`, `useUpdateUser`, `useAllowedIdentities`, `useAddAllowedIdentity`, `useDeleteAllowedIdentity` (new file), `useUnlinkIdentity` (added to `useMe.ts`) — all consumed by Tasks 14-15.

- [ ] **Step 1: Update `types.ts`**

In `packages/webui/src/lib/types.ts`, add after the existing `Me` interface:

```typescript
export interface UserIdentitySummary {
  id: number;
  provider: string;
  raw_id: string;
  display_name: string | null;
}
```

Change `Me` to:

```typescript
export interface Me {
  username: string;
  is_admin: boolean;
  display_name: string | null;
  identities: UserIdentitySummary[];
}
```

Add after `AuthProviders`:

```typescript
export interface User {
  id: number;
  is_admin: boolean;
  allowed: boolean;
  created_at: number;
  identities: UserIdentitySummary[];
}

export interface UpdateUserInput {
  is_admin?: boolean;
  allowed?: boolean;
}

export interface AllowedIdentity {
  id: number;
  provider: string;
  raw_id: string;
  grant_admin: boolean;
}

export interface AddAllowedIdentityInput {
  provider: string;
  raw_id: string;
  grant_admin: boolean;
}
```

- [ ] **Step 2: Update `api.ts`**

In `packages/webui/src/lib/api.ts`, add `AddAllowedIdentityInput`, `AllowedIdentity`, `UpdateUserInput`, `User` to the `import type { ... } from "./types"` block. Add these entries to the `api` object, after `authProviders`:

```typescript
  unlinkIdentity: (id: number) =>
    request<{ deleted: number }>(`/api/me/identities/${id}`, { method: "DELETE" }),
  listUsers: () => request<User[]>("/api/users"),
  updateUser: (id: number, input: UpdateUserInput) =>
    request<User>(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify(input) }),
  listAllowedIdentities: () => request<AllowedIdentity[]>("/api/allowed-identities"),
  addAllowedIdentity: (input: AddAllowedIdentityInput) =>
    request<AllowedIdentity>("/api/allowed-identities", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  deleteAllowedIdentity: (id: number) =>
    request<{ deleted: number }>(`/api/allowed-identities/${id}`, { method: "DELETE" }),
```

- [ ] **Step 3: Create `hooks/useUsers.ts`**

Create `packages/webui/src/hooks/useUsers.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { AddAllowedIdentityInput, UpdateUserInput } from "@/lib/types";

const usersKey = ["users"] as const;
const allowedIdentitiesKey = ["allowed-identities"] as const;

export function useUsers() {
  return useQuery({ queryKey: usersKey, queryFn: api.listUsers });
}

export function useUpdateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, input }: { id: number; input: UpdateUserInput }) =>
      api.updateUser(id, input),
    onSuccess: () => qc.invalidateQueries({ queryKey: usersKey }),
  });
}

export function useAllowedIdentities() {
  return useQuery({ queryKey: allowedIdentitiesKey, queryFn: api.listAllowedIdentities });
}

export function useAddAllowedIdentity() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: AddAllowedIdentityInput) => api.addAllowedIdentity(input),
    onSuccess: () => qc.invalidateQueries({ queryKey: allowedIdentitiesKey }),
  });
}

export function useDeleteAllowedIdentity() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.deleteAllowedIdentity(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: allowedIdentitiesKey }),
  });
}
```

- [ ] **Step 4: Add `useUnlinkIdentity` to `hooks/useMe.ts`**

In `packages/webui/src/hooks/useMe.ts`, change the import line to `import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";` and append:

```typescript
export function useUnlinkIdentity() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.unlinkIdentity(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["me"] }),
  });
}
```

- [ ] **Step 5: Build and lint**

Run: `cd packages/webui && pnpm build && pnpm lint`
Expected: both clean (this task adds no components, so there's nothing to visually verify yet — Tasks 14-15 consume these types/hooks).

- [ ] **Step 6: Commit**

```bash
cd packages/webui
git add src/lib/types.ts src/lib/api.ts src/hooks/useUsers.ts src/hooks/useMe.ts
git commit -m "feat(webui): add types/api/hooks for account linking and user management"
```

---

### Task 14: Webui — Account page linking UI

**Files:**
- Modify: `packages/webui/src/components/AccountPage.tsx`

**Interfaces:**
- Consumes: `Me.identities` (Task 13), `useAuthProviders` (existing, from the Steam-login feature), `useUnlinkIdentity` (Task 13).

- [ ] **Step 1: Update `AccountPage.tsx`**

Read `packages/webui/src/components/AccountPage.tsx` in full first (it's short — the personal-token section at the bottom is unrelated and must stay unchanged). Add imports and a new section between the existing username/admin-badge block and the "Personal token" section:

```typescript
import { useState } from "react";
import { useMe } from "@/hooks/useMe";
import { useUnlinkIdentity } from "@/hooks/useMe";
import { useAuthProviders } from "@/hooks/useAuthProviders";
import { useGenerateToken } from "@/hooks/useToken";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

const PROVIDER_LABELS: Record<string, string> = {
  github: "GitHub",
  steam: "Steam",
};

export function AccountPage() {
  const { data: me } = useMe();
  const { data: providers } = useAuthProviders();
  const unlinkIdentity = useUnlinkIdentity();
  const generateToken = useGenerateToken();
  const [token, setToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const linkedProviders = new Set((me?.identities ?? []).map((i) => i.provider));
  const linkable = Object.entries(providers ?? {})
    .filter(([slug, on]) => on && !linkedProviders.has(slug))
    .map(([slug]) => slug);

  return (
    <div className="max-w-lg space-y-6">
      <div>
        <h1 className="text-xl font-semibold">My account</h1>
        <div className="mt-2 flex items-center gap-2 text-sm text-muted-foreground">
          <span>{me?.display_name ?? me?.username}</span>
          {me?.is_admin ? <Badge>Admin</Badge> : null}
        </div>
      </div>
      <div className="space-y-2">
        <h2 className="text-sm font-medium">Linked identities</h2>
        <p className="text-sm text-muted-foreground">
          Sign in with either linked identity — they reach the same account.
        </p>
        <ul className="space-y-1">
          {(me?.identities ?? []).map((identity) => (
            <li key={identity.id} className="flex items-center justify-between text-sm">
              <span>
                {PROVIDER_LABELS[identity.provider] ?? identity.provider}:{" "}
                {identity.display_name ?? identity.raw_id}
              </span>
              <Button
                variant="outline"
                size="sm"
                disabled={unlinkIdentity.isPending || (me?.identities.length ?? 0) <= 1}
                onClick={() => unlinkIdentity.mutate(identity.id)}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
        {unlinkIdentity.isError ? (
          <p className="text-sm text-destructive">{unlinkIdentity.error.message}</p>
        ) : null}
        {linkable.length > 0 ? (
          <div className="flex gap-2 pt-1">
            {linkable.map((slug) => (
              <a
                key={slug}
                href={`/admin/link/${slug}`}
                className="rounded-md border px-3 py-1.5 text-sm"
              >
                Link {PROVIDER_LABELS[slug] ?? slug}
              </a>
            ))}
          </div>
        ) : null}
      </div>
      <div className="space-y-2">
        <h2 className="text-sm font-medium">Personal token</h2>
        <p className="text-sm text-muted-foreground">
          Use this for MCP clients that can't do a browser login (e.g. Claude
          Desktop). Generating a new token immediately invalidates any
          previous one.
        </p>
        <Button
          onClick={async () => {
            const result = await generateToken.mutateAsync();
            setToken(result.token);
            setCopied(false);
          }}
          disabled={generateToken.isPending}
        >
          {token ? "Regenerate token" : "Generate token"}
        </Button>
        {generateToken.isError ? (
          <p className="text-sm text-destructive">{generateToken.error.message}</p>
        ) : null}
        {token ? (
          <div className="space-y-1">
            <p className="text-sm font-medium">Copy this now — it won't be shown again:</p>
            <pre className="overflow-x-auto rounded-md border bg-muted p-2 text-xs">{token}</pre>
            <Button
              variant="outline"
              size="sm"
              onClick={async () => {
                await navigator.clipboard.writeText(token);
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              }}
            >
              {copied ? "Copied!" : "Copy"}
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
```

(Note the `import { useState } from "react";` and `useGenerateToken`/`Badge`/`Button` imports already existed in the original file — the block above is the complete new file content, not a diff, since so much of the top of the file changes.)

- [ ] **Step 2: Build and lint**

Run: `cd packages/webui && pnpm build && pnpm lint`
Expected: both clean.

- [ ] **Step 3: Manual verification**

Run `just webui-dev`, log in, and confirm: the Account page shows the current identity under "Linked identities", a "Link Steam" (or "Link GitHub", whichever isn't the one you're logged in as) button appears if that provider is configured, and the "Remove" button is disabled when only one identity is linked.

- [ ] **Step 4: Commit**

```bash
cd packages/webui
git add src/components/AccountPage.tsx
git commit -m "feat(webui): show linked identities and link/unlink controls on Account page"
```

---

### Task 15: Webui — Users admin page + routing + nav link

**Files:**
- Create: `packages/webui/src/components/UsersPage.tsx`
- Modify: `packages/webui/src/router.tsx`
- Modify: `packages/webui/src/components/AppLayout.tsx`

**Interfaces:**
- Consumes: `useUsers`, `useUpdateUser`, `useAllowedIdentities`, `useAddAllowedIdentity`, `useDeleteAllowedIdentity` (Task 13).

- [ ] **Step 1: Create `UsersPage.tsx`**

Create `packages/webui/src/components/UsersPage.tsx`:

```typescript
import { useState } from "react";
import type { FormEvent } from "react";
import { useMe } from "@/hooks/useMe";
import {
  useAddAllowedIdentity,
  useAllowedIdentities,
  useDeleteAllowedIdentity,
  useUpdateUser,
  useUsers,
} from "@/hooks/useUsers";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function UsersPage() {
  const { data: me } = useMe();

  if (!me?.is_admin) {
    return <p className="text-sm text-muted-foreground">Admins only.</p>;
  }

  return (
    <div className="space-y-8">
      <UsersTable currentUserId={idFromUsername(me.username)} />
      <AllowedIdentitiesSection />
    </div>
  );
}

function idFromUsername(username: string): number {
  return Number(username.replace("user:", ""));
}

function UsersTable({ currentUserId }: { currentUserId: number }) {
  const { data: users } = useUsers();
  const updateUser = useUpdateUser();

  return (
    <div className="space-y-2">
      <h2 className="text-lg font-semibold">Users</h2>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Identities</TableHead>
            <TableHead>Admin</TableHead>
            <TableHead>Allowed</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {(users ?? []).map((user) => {
            const isSelf = user.id === currentUserId;
            return (
              <TableRow key={user.id}>
                <TableCell className="space-x-1">
                  {user.identities.map((identity) => (
                    <Badge key={identity.id} variant="outline">
                      {identity.provider}:{identity.display_name ?? identity.raw_id}
                    </Badge>
                  ))}
                </TableCell>
                <TableCell>{user.is_admin ? "Yes" : "No"}</TableCell>
                <TableCell>{user.allowed ? "Yes" : "No"}</TableCell>
                <TableCell className="space-x-2 text-right">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={isSelf && user.is_admin || updateUser.isPending}
                    onClick={() =>
                      updateUser.mutate({ id: user.id, input: { is_admin: !user.is_admin } })
                    }
                  >
                    {user.is_admin ? "Revoke admin" : "Make admin"}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={isSelf && user.allowed || updateUser.isPending}
                    onClick={() =>
                      updateUser.mutate({ id: user.id, input: { allowed: !user.allowed } })
                    }
                  >
                    {user.allowed ? "Disable" : "Enable"}
                  </Button>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
      {updateUser.isError ? (
        <p className="text-sm text-destructive">{updateUser.error.message}</p>
      ) : null}
    </div>
  );
}

function AllowedIdentitiesSection() {
  const { data: rows } = useAllowedIdentities();
  const addRow = useAddAllowedIdentity();
  const deleteRow = useDeleteAllowedIdentity();
  const [provider, setProvider] = useState("github");
  const [rawId, setRawId] = useState("");
  const [grantAdmin, setGrantAdmin] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    await addRow.mutateAsync({ provider, raw_id: rawId, grant_admin: grantAdmin });
    setRawId("");
    setGrantAdmin(false);
  }

  return (
    <div className="space-y-2">
      <h2 className="text-lg font-semibold">Pending identities</h2>
      <p className="text-sm text-muted-foreground">
        Pre-approve a raw GitHub login or SteamID64 before that person has logged in.
        Leave both allow-lists empty for a provider to let anyone with that provider sign
        in.
      </p>
      <form className="flex items-end gap-2" onSubmit={handleSubmit}>
        <div className="space-y-1">
          <Label>Provider</Label>
          <Select value={provider} onValueChange={setProvider}>
            <SelectTrigger className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="github">GitHub</SelectItem>
              <SelectItem value="steam">Steam</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="raw-id">Raw ID</Label>
          <Input
            id="raw-id"
            value={rawId}
            onChange={(e) => setRawId(e.target.value)}
            placeholder="octocat or 76561198012345678"
            required
          />
        </div>
        <label className="flex items-center gap-1 pb-2 text-sm">
          <input
            type="checkbox"
            checked={grantAdmin}
            onChange={(e) => setGrantAdmin(e.target.checked)}
          />
          Grant admin
        </label>
        <Button type="submit" disabled={addRow.isPending}>
          Add
        </Button>
      </form>
      {addRow.isError ? <p className="text-sm text-destructive">{addRow.error.message}</p> : null}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Provider</TableHead>
            <TableHead>Raw ID</TableHead>
            <TableHead>Grants admin</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {(rows ?? []).map((row) => (
            <TableRow key={row.id}>
              <TableCell>{row.provider}</TableCell>
              <TableCell>{row.raw_id}</TableCell>
              <TableCell>{row.grant_admin ? "Yes" : "No"}</TableCell>
              <TableCell className="text-right">
                <Button
                  size="sm"
                  variant="destructive"
                  disabled={deleteRow.isPending}
                  onClick={() => deleteRow.mutate(row.id)}
                >
                  Remove
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
```

- [ ] **Step 2: Add the `/users` route**

In `packages/webui/src/router.tsx`, add the import `import { UsersPage } from "@/components/UsersPage";` and, after `accountRoute`:

```typescript
export const usersRoute = createRoute({
  getParentRoute: () => authedLayoutRoute,
  path: "/users",
  component: UsersPage,
});
```

Add `usersRoute` to the `addChildren` array: `authedLayoutRoute.addChildren([serversRoute, logsRoute, testerRoute, accountRoute, usersRoute])`.

- [ ] **Step 3: Add the nav link, admin-only**

In `packages/webui/src/components/AppLayout.tsx`, add a `Link` to `/users` right after the `/account` link, shown only when `me?.is_admin`:

```typescript
            <Link
              to="/account"
              activeProps={{ className: "font-semibold text-foreground" }}
              className="text-sm text-muted-foreground"
            >
              Account
            </Link>
            {me?.is_admin ? (
              <Link
                to="/users"
                activeProps={{ className: "font-semibold text-foreground" }}
                className="text-sm text-muted-foreground"
              >
                Users
              </Link>
            ) : null}
```

- [ ] **Step 4: Build and lint**

Run: `cd packages/webui && pnpm build && pnpm lint`
Expected: both clean.

- [ ] **Step 5: Manual verification**

Run `just webui-dev`, log in as an admin, and confirm: the "Users" nav link appears, the Users page lists accounts with working admin/allowed toggles (and the self-lockout guard disables the buttons on your own row), and the "Pending identities" form successfully adds/removes `AllowedIdentity` rows. Log in as a non-admin (or visit `/admin/users` directly) and confirm the "Admins only" fallback shows instead of the table.

- [ ] **Step 6: Commit**

```bash
cd packages/webui
git add src/components/UsersPage.tsx src/router.tsx src/components/AppLayout.tsx
git commit -m "feat(webui): add admin Users page for account/allow-list management"
```

---

### Task 16: Docs

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `CLAUDE.md` (repo root)

**Interfaces:** None — documentation only.

- [ ] **Step 1: Update `README.md`**

Read the current `README.md` in full first (it already has an "Upgrading" section from the Steam-login feature, an "Auth model" bullet list, and an "Administration" → "Web UI" section — this task extends all three, doesn't replace them).

Add a new bullet to the "Auth model" list (after the existing "Identity across providers" bullet):

```markdown
- **Account linking** — while logged in, link a second provider from the Account page (self-service; both identities must be logged into directly, no admin override). Linked identities reach the same account either way.
```

Extend the existing "Upgrading" section with a new bullet:

```markdown
- **Allow-lists and `ADMIN_USERS` become one-time seed values.** On first startup after upgrading, `GITHUB_ALLOWED_USERS`/`STEAM_ALLOWED_USERS`/`ADMIN_USERS` are read once into the database and then have **no further effect** — manage allow-lists and admin rights from the webui's Users page from then on. Editing these in `.env`/Coolify after the first startup does nothing.
```

Add a new subsection under "Administration" → "Web UI" (after the existing bullet list):

```markdown
### User management (admins)

The **Users** nav link (admin-only) shows every account, its linked
identities, and toggles for admin rights and whether the account may still
log in. A second section manages the "pending identities" allow-list — add
a raw GitHub login or SteamID64 there to pre-approve someone before they've
ever logged in (optionally granting admin immediately). Leave a provider's
allow-list empty to let anyone with that provider sign in, same as the old
env-var default.
```

- [ ] **Step 2: Update `.env.example`**

Read the current `.env.example` in full first. Extend the existing comment block above `GITHUB_ALLOWED_USERS`/`ADMIN_USERS`/`STEAM_ALLOWED_USERS` (already flagged as an upgrade-sensitive area from the Steam-login work) with one sentence noting these are now one-time seed values only, e.g.:

```
# These three are read ONCE on first startup to seed the database, then
# have no further effect -- manage allow-lists and admin rights from the
# webui's Users page after that. Editing these here later does nothing.
```

- [ ] **Step 3: Update `CLAUDE.md`**

Read `CLAUDE.md` at the repo root in full first. If it documents the auth env vars or the DB schema/migration approach anywhere, add one line noting that user/allow-list management moved to the DB via `api/users_router.py`, seeded once from env vars — otherwise, if it doesn't currently mention auth config specifics, skip this file (don't add a section that wasn't there before; this repo's `CLAUDE.md` conventions favor terse, load-bearing notes only).

- [ ] **Step 4: Commit**

```bash
git add README.md .env.example CLAUDE.md
git commit -m "docs: document account linking and DB-backed user management"
```

---

## Final Verification

- [ ] Full backend suite: `cd packages/aggregator && uv run pytest -q` — 100% pass.
- [ ] Lint/format: `cd packages/aggregator && uvx ruff check src tests && uvx ruff format --check src tests` — both clean.
- [ ] Webui: `cd packages/webui && pnpm build && pnpm lint` — both clean.
- [ ] Grep for stray references to the removed `access_control.is_allowed` and the removed `GITHUB_ALLOWED_USERS`/`STEAM_ALLOWED_USERS`/`ADMIN_USERS` imports in `access_control.py`: `grep -rn "is_allowed\b" packages/aggregator/src` should show nothing (the string `is_allowed` should no longer exist anywhere in `src/`).
- [ ] Manual smoke test: fresh `DATA_DIR`, boot the app, confirm `ADMIN_USERS`/`GITHUB_ALLOWED_USERS` seed correctly on first boot, log in, link a second provider, confirm both identities reach the same account, use the Users page to disable an account and confirm it's immediately locked out.
