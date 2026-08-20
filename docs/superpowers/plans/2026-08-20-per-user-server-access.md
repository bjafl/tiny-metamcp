# Per-user server access control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an admin role, per-server visibility (everyone vs. owner-private), and personal API tokens that replace the shared `ADMIN_TOKEN`, so each user only sees/uses the MCP servers they're allowed to.

**Architecture:** Two new DB columns on `Server` (`owner_username`, `visibility`) plus a new `PersonalToken` table. A single new `access_control.py` module is the one place that knows the visibility/ownership rules; it's imported by the REST API, the MCP meta-tools, and the `/mcp` tool-list/dispatch handlers so the rule isn't duplicated three times. Per-request identity flows into the shared, stateless `/mcp` handler via a `contextvars.ContextVar`, set once per request by the bearer-auth check.

**Tech Stack:** FastAPI, SQLModel/SQLAlchemy (async, SQLite), the low-level `mcp.server.Server`, React/TanStack Query/Router (webui).

**Spec:** `docs/superpowers/specs/2026-08-20-per-user-server-access-design.md`

## Global Constraints

- `ADMIN_TOKEN` is removed entirely — no compatibility shim, no fallback branch.
- A new server defaults to `visibility="private"` unless the caller explicitly asks for `"everyone"`. Pre-existing servers (from before this migration) default to `visibility="everyone"`, `owner_username=NULL` — this preserves current behavior for them but means only admins can manage them going forward (no owner to match).
- Management rights (`edit_server`/`delete_server`/`enable_server`/`disable_server`/`restart_server`, and the REST equivalents) require `can_manage` (admin or owner). Read/use rights (`list_servers`, `/mcp` tool visibility, `/api/tools`, `/api/tools/call`, `/api/logs*`) require only visibility.
- A caller without access to a server gets the same "not found" response a truly nonexistent server would produce (`ValueError("No server named ...")` for meta-tools, HTTP 404 for the REST API) — never 403. This avoids confirming a private server's existence to users who can't see it.
- One personal token per user; generating a new one immediately invalidates the previous one. Only its SHA-256 hash is ever stored.
- Every `uv run` command below runs from `packages/aggregator/` (`cd packages/aggregator && uv run ...`) per this repo's workspace layout.

---

## Task 1: Fix the pre-existing `except X, Y:` syntax bug in `admin_auth.py`

This file has `except BadSignature, SignatureExpired:` (Python 2 syntax) at two call sites — a `SyntaxError` under Python 3 that would prevent the module from importing at all. It's a prerequisite: Task 5 below modifies this same file's `require_api_auth`, and nothing in it is testable while it fails to import.

**Files:**
- Modify: `packages/aggregator/src/aggregator/admin_auth.py:42`, `packages/aggregator/src/aggregator/admin_auth.py:108`
- Test: `packages/aggregator/tests/test_admin_auth.py` (new)

**Interfaces:**
- Produces: `admin_auth.get_session_user(request) -> str | None` (unchanged behavior, now actually importable).

- [ ] **Step 1: Write the failing test**

Create `packages/aggregator/tests/test_admin_auth.py`:

```python
"""
Regression test for the admin_auth module actually being importable and
get_session_user() correctly rejecting a garbage/expired session cookie
without raising -- it previously used Python 2 `except X, Y:` syntax, a
SyntaxError under Python 3 that made the whole module fail to import.
"""

from fastapi import Request

from aggregator.admin_auth import get_session_user


def _request_with_cookie(cookie_value: str) -> Request:
    scope = {
        "type": "http",
        "headers": [(b"cookie", f"admin_session={cookie_value}".encode())],
    }
    return Request(scope)


def test_get_session_user_returns_none_for_garbage_cookie():
    assert get_session_user(_request_with_cookie("not-a-real-signed-value")) is None


def test_get_session_user_returns_none_when_no_cookie():
    assert get_session_user(Request({"type": "http", "headers": []})) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd packages/aggregator && uv run pytest tests/test_admin_auth.py -v`
Expected: FAIL — collection error, `SyntaxError: invalid syntax` pointing at the `except BadSignature, SignatureExpired:` lines.

- [ ] **Step 3: Fix the syntax**

In `admin_auth.py`, change both occurrences:

```python
    except (BadSignature, SignatureExpired):
        return None
```

(line 42, inside `get_session_user`) and:

```python
    except (BadSignature, SignatureExpired):
        return _login_error("Invalid or expired state — please try again")
```

(line 108, inside `handle_callback`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd packages/aggregator && uv run pytest tests/test_admin_auth.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add packages/aggregator/src/aggregator/admin_auth.py packages/aggregator/tests/test_admin_auth.py
git commit -m "fix(aggregator): fix Python 2 except syntax in admin_auth.py"
```

---

## Task 2: Data model — `Server.owner_username`/`visibility`, `PersonalToken`, migration

**Files:**
- Modify: `packages/aggregator/src/aggregator/models.py`
- Modify: `packages/aggregator/src/aggregator/database.py`
- Test: `packages/aggregator/tests/test_database.py`

**Interfaces:**
- Produces: `models.ServerVisibility` (StrEnum: `EVERYONE = "everyone"`, `PRIVATE = "private"`); `Server.owner_username: str | None`, `Server.visibility: str`; `models.PersonalToken` (table: `username` PK, `token_hash` unique, `created_at`).
- Produces: `database.add_server(..., owner_username=None, visibility=ServerVisibility.PRIVATE.value)`; `database.update_server(..., visibility=None)`; `database._migrate_server_columns(conn: AsyncConnection) -> None`; `database.set_personal_token(username, token_hash) -> None`; `database.get_username_by_token_hash(token_hash) -> str | None`.

- [ ] **Step 1: Write the failing tests**

In `packages/aggregator/tests/test_database.py`, replace the existing two import lines (`import pytest` stays; `from aggregator.database import add_server, delete_server, update_server` and `from aggregator.models import ServerType` are replaced) with:

```python
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
```

Then append the new test functions below the existing ones:

Add these test functions:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_database.py -v -k "visibility or personal_token or migrate"`
Expected: FAIL — `ImportError`/`AttributeError` (`ServerVisibility`, `owner_username`, `_migrate_server_columns`, `set_personal_token`, `get_username_by_token_hash` don't exist yet).

- [ ] **Step 3: Add the model classes**

In `packages/aggregator/src/aggregator/models.py`, add `ServerVisibility` next to `ServerType`, and the two new `Server` fields:

```python
class ServerVisibility(StrEnum):
    EVERYONE = "everyone"
    PRIVATE = "private"


class Server(SQLModel, table=True):
    __tablename__ = "servers"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    type: str
    package: str
    args: str = Field(default="[]")  # JSON array
    env: str = Field(default="{}")  # JSON object
    enabled: bool = Field(default=True)
    owner_username: str | None = Field(default=None)
    visibility: str = Field(default=ServerVisibility.EVERYONE.value)

    def get_args(self) -> list[str]:
        return json.loads(self.args)

    def get_env(self) -> dict[str, str]:
        return json.loads(self.env)
```

(`visibility`'s column-level default is `"everyone"` on purpose — it matches the value the migration backfills onto pre-existing rows in Step 5 below. New rows always pass `visibility` explicitly through `add_server`, which defaults to `"private"` at the Python level, so this column default is only ever actually used by the migration path.)

Add `PersonalToken` at the end of the file:

```python
class PersonalToken(SQLModel, table=True):
    __tablename__ = "personal_tokens"

    username: str = Field(primary_key=True)
    token_hash: str = Field(unique=True, index=True)
    created_at: float = Field(default_factory=_time.time)
```

- [ ] **Step 4: Extend `database.py`'s `add_server`/`update_server`**

In `packages/aggregator/src/aggregator/database.py`, update the import block:

```python
from .models import (  # noqa: F401 – re-exported for callers
    OAuthToken,
    PersonalToken,
    Server,
    ServerType,
    ServerVisibility,
)
```

Change `add_server`'s signature and body:

```python
async def add_server(
    name: str,
    server_type: ServerType,
    package: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    owner_username: str | None = None,
    visibility: str = ServerVisibility.PRIVATE.value,
) -> Server:
    server = Server(
        name=name,
        type=server_type.value if isinstance(server_type, ServerType) else server_type,
        package=package,
        args=json.dumps(args or []),
        env=json.dumps(env or {}),
        owner_username=owner_username,
        visibility=visibility,
    )
    async with _session_factory() as session:
        session.add(server)
        await session.commit()
        await session.refresh(server)
    return server
```

In `update_server`, add the `visibility` parameter and apply it alongside the existing fields:

```python
async def update_server(
    server_id: int,
    name: str | None = None,
    server_type: ServerType | None = None,
    package: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    visibility: str | None = None,
) -> Server | None:
    try:
        async with _session_factory() as session:
            server = await session.get(Server, server_id)
            if not server:
                return None
            if name is not None:
                server.name = name
            if server_type is not None:
                server.type = (
                    server_type.value if isinstance(server_type, ServerType) else server_type
                )
            if package is not None:
                server.package = package
            if args is not None:
                server.args = json.dumps(args)
            if env is not None:
                server.env = json.dumps(env)
            if visibility is not None:
                server.visibility = visibility
            await session.commit()
            await session.refresh(server)
            return server
    except IntegrityError as exc:
        raise ValueError(f"A server named {name!r} already exists") from exc
```

- [ ] **Step 5: Add the migration step and personal-token CRUD**

Add `from sqlalchemy import inspect, text` and `from sqlalchemy.ext.asyncio import AsyncConnection` to the top imports (alongside the existing `sqlalchemy.ext.asyncio` import line — merge into one `from sqlalchemy.ext.asyncio import (...)` import). Add, above `init_db()`:

```python
async def _migrate_server_columns(conn: AsyncConnection) -> None:
    """Add columns introduced after a deployment's `servers` table was first
    created -- `SQLModel.metadata.create_all` only creates missing tables,
    it never alters an existing one."""

    def _sync(sync_conn):
        existing = {col["name"] for col in inspect(sync_conn).get_columns("servers")}
        if "owner_username" not in existing:
            sync_conn.execute(text("ALTER TABLE servers ADD COLUMN owner_username TEXT"))
        if "visibility" not in existing:
            sync_conn.execute(
                text("ALTER TABLE servers ADD COLUMN visibility TEXT DEFAULT 'everyone'")
            )

    await conn.run_sync(_sync)
```

Change `init_db()`:

```python
async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
        await _migrate_server_columns(conn)
```

Add, near the bottom of the file (a new `# ── Personal tokens ──` section):

```python
# ── Personal tokens ──────────────────────────────────────────────────────────


async def set_personal_token(username: str, token_hash: str) -> None:
    async with _session_factory() as session:
        existing = await session.get(PersonalToken, username)
        if existing:
            existing.token_hash = token_hash
        else:
            session.add(PersonalToken(username=username, token_hash=token_hash))
        await session.commit()


async def get_username_by_token_hash(token_hash: str) -> str | None:
    async with _session_factory() as session:
        result = await session.execute(
            select(PersonalToken).where(PersonalToken.token_hash == token_hash)
        )
        token = result.scalar_one_or_none()
        return token.username if token else None
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_database.py -v`
Expected: PASS (all tests, old and new)

- [ ] **Step 7: Commit**

```bash
git add packages/aggregator/src/aggregator/models.py packages/aggregator/src/aggregator/database.py packages/aggregator/tests/test_database.py
git commit -m "feat(aggregator): add Server owner/visibility columns and PersonalToken table"
```

---

## Task 3: `config.py` — retire `ADMIN_TOKEN`, add `ADMIN_USERS`

**Files:**
- Modify: `packages/aggregator/src/aggregator/config.py`

**Interfaces:**
- Produces: `config.ADMIN_USERS: set[str]`
- Removes: `config.ADMIN_TOKEN`

No dedicated test file — this is a two-line config change; its behavior is exercised by every later task's tests (`access_control.is_admin`, `main.py`'s bearer check no longer accepting `ADMIN_TOKEN`).

- [ ] **Step 1: Edit `config.py`**

Replace:

```python
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
```

with:

```python
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
```

Add, after the existing `GITHUB_ALLOWED_USERS` block:

```python
ADMIN_USERS: set[str] = {
    u.strip() for u in os.getenv("ADMIN_USERS", "").split(",") if u.strip()
}
```

- [ ] **Step 2: Verify the module still imports**

Run: `cd packages/aggregator && uv run python -c "from aggregator import config; print(config.ADMIN_USERS)"`
Expected: prints `set()` (empty, since `ADMIN_USERS` isn't set in this shell)

- [ ] **Step 3: Commit**

```bash
git add packages/aggregator/src/aggregator/config.py
git commit -m "feat(aggregator): replace ADMIN_TOKEN with ADMIN_USERS config"
```

---

## Task 4: New `access_control.py` module

The single source of truth for visibility/ownership rules and personal-token hashing, imported by everything downstream.

**Files:**
- Create: `packages/aggregator/src/aggregator/access_control.py`
- Test: `packages/aggregator/tests/test_access_control.py` (new)

**Interfaces:**
- Consumes: `config.ADMIN_USERS: set[str]` (Task 3); `database.list_servers()`, `database.set_personal_token()`, `database.get_username_by_token_hash()` (Task 2); `models.Server`, `models.ServerVisibility` (Task 2).
- Produces: `is_admin(username: str) -> bool`; `can_manage(server: Server, username: str) -> bool`; `visible_servers(username: str) -> list[Server]`; `visible_server_names(username: str) -> set[str]`; `generate_personal_token(username: str) -> str`; `validate_personal_token(token: str) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Create `packages/aggregator/tests/test_access_control.py`:

```python
"""
Unit tests for the visibility/ownership/personal-token rules in
access_control.py -- the single module every other access-control
enforcement point (meta_tools, routers, /mcp) delegates to.
"""

from aggregator import access_control
from aggregator.database import add_server, delete_server
from aggregator.models import ServerType, ServerVisibility

ADMIN = "test-admin"  # must be in ADMIN_USERS, set by conftest.py
OWNER = "ac-owner"
STRANGER = "ac-stranger"


async def _cleanup(server_id: int) -> None:
    await delete_server(server_id)


def test_is_admin_true_only_for_admin_users_env():
    assert access_control.is_admin(ADMIN)
    assert not access_control.is_admin(OWNER)


async def test_can_manage_true_for_owner_and_admin_false_for_stranger():
    server = await add_server(
        "ac-can-manage",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=OWNER,
        visibility=ServerVisibility.PRIVATE.value,
    )
    try:
        assert access_control.can_manage(server, OWNER)
        assert access_control.can_manage(server, ADMIN)
        assert not access_control.can_manage(server, STRANGER)
    finally:
        await _cleanup(server.id)


async def test_visible_servers_private_only_to_owner_and_admin():
    server = await add_server(
        "ac-visible-private",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=OWNER,
        visibility=ServerVisibility.PRIVATE.value,
    )
    try:
        owner_names = {s.name for s in await access_control.visible_servers(OWNER)}
        admin_names = {s.name for s in await access_control.visible_servers(ADMIN)}
        stranger_names = {s.name for s in await access_control.visible_servers(STRANGER)}
        assert server.name in owner_names
        assert server.name in admin_names
        assert server.name not in stranger_names
    finally:
        await _cleanup(server.id)


async def test_visible_servers_everyone_visible_to_all():
    server = await add_server(
        "ac-visible-everyone",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=OWNER,
        visibility=ServerVisibility.EVERYONE.value,
    )
    try:
        stranger_names = {s.name for s in await access_control.visible_servers(STRANGER)}
        assert server.name in stranger_names
    finally:
        await _cleanup(server.id)


async def test_visible_server_names_matches_visible_servers():
    server = await add_server(
        "ac-visible-names",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=OWNER,
        visibility=ServerVisibility.EVERYONE.value,
    )
    try:
        names = await access_control.visible_server_names(STRANGER)
        assert server.name in names
    finally:
        await _cleanup(server.id)


async def test_generate_and_validate_personal_token_round_trip():
    token = await access_control.generate_personal_token("token-user")
    assert await access_control.validate_personal_token(token) == "token-user"


async def test_regenerating_token_invalidates_previous_one():
    old_token = await access_control.generate_personal_token("token-user-2")
    new_token = await access_control.generate_personal_token("token-user-2")
    assert await access_control.validate_personal_token(old_token) is None
    assert await access_control.validate_personal_token(new_token) == "token-user-2"


async def test_validate_personal_token_unknown_token_returns_none():
    assert await access_control.validate_personal_token("not-a-real-token") is None
```

This references `ADMIN_USERS=test-admin`, which Task 11 adds to `conftest.py`. Until then, `test_is_admin_true_only_for_admin_users_env` and `test_can_manage_true_for_owner_and_admin_false_for_stranger` will fail even after `access_control.py` exists — that's expected and gets fixed by Task 11, not this task. Skip those two assertions' pass/fail status when checking this task's own Step 4 below; the rest must pass.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_access_control.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aggregator.access_control'`

- [ ] **Step 3: Implement `access_control.py`**

Create `packages/aggregator/src/aggregator/access_control.py`:

```python
"""
Single source of truth for who can see and manage which MCP servers, and
for personal API token hashing/validation. Imported by the REST API
(routers.py), the MCP meta-tools (meta_tools.py), and the /mcp tool-list/
dispatch handlers (aggregator.py) -- the rule lives here exactly once.
"""

import hashlib
import secrets

from .config import ADMIN_USERS
from .database import (
    get_username_by_token_hash,
    list_servers,
    set_personal_token,
)
from .models import Server, ServerVisibility


def is_admin(username: str) -> bool:
    return username in ADMIN_USERS


def can_manage(server: Server, username: str) -> bool:
    return is_admin(username) or server.owner_username == username


def _is_visible(server: Server, username: str) -> bool:
    if is_admin(username):
        return True
    if server.visibility == ServerVisibility.EVERYONE.value:
        return True
    return server.owner_username == username


async def visible_servers(username: str) -> list[Server]:
    return [s for s in await list_servers() if _is_visible(s, username)]


async def visible_server_names(username: str) -> set[str]:
    return {s.name for s in await visible_servers(username)}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def generate_personal_token(username: str) -> str:
    token = secrets.token_urlsafe(32)
    await set_personal_token(username, _hash_token(token))
    return token


async def validate_personal_token(token: str) -> str | None:
    return await get_username_by_token_hash(_hash_token(token))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_access_control.py -v`
Expected: 7 of 9 PASS; `test_is_admin_true_only_for_admin_users_env` and
`test_can_manage_true_for_owner_and_admin_false_for_stranger` FAIL (no
`ADMIN_USERS` env set yet — expected until Task 11).

- [ ] **Step 5: Commit**

```bash
git add packages/aggregator/src/aggregator/access_control.py packages/aggregator/tests/test_access_control.py
git commit -m "feat(aggregator): add access_control module for visibility/ownership rules"
```

---

## Task 5: `admin_auth.require_api_auth` resolves and returns a username

**Files:**
- Modify: `packages/aggregator/src/aggregator/admin_auth.py`
- Test: `packages/aggregator/tests/test_admin_auth.py`

**Interfaces:**
- Consumes: `access_control.validate_personal_token(token) -> str | None` (Task 4).
- Produces: `admin_auth.require_api_auth(request) -> str` (was `-> None`; now async, raises `HTTPException(401)` on failure, returns username on success).

- [ ] **Step 1: Write the failing test**

Append to `packages/aggregator/tests/test_admin_auth.py`:

```python
import pytest
from fastapi import HTTPException

from aggregator import access_control
from aggregator.admin_auth import require_api_auth


def _request_with_headers(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


async def test_require_api_auth_accepts_valid_personal_token():
    token = await access_control.generate_personal_token("auth-test-user")
    username = await require_api_auth(_request_with_headers({"authorization": f"Bearer {token}"}))
    assert username == "auth-test-user"


async def test_require_api_auth_rejects_unknown_bearer_token():
    with pytest.raises(HTTPException) as exc_info:
        await require_api_auth(_request_with_headers({"authorization": "Bearer not-a-real-token"}))
    assert exc_info.value.status_code == 401


async def test_require_api_auth_rejects_missing_auth():
    with pytest.raises(HTTPException) as exc_info:
        await require_api_auth(Request({"type": "http", "headers": []}))
    assert exc_info.value.status_code == 401
```

(`Request` is already imported at the top of this file from Task 1.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd packages/aggregator && uv run pytest tests/test_admin_auth.py -v -k require_api_auth`
Expected: FAIL — `require_api_auth` currently only accepts a session cookie or `ADMIN_TOKEN` (already removed in Task 3, so this errors on the missing import) and returns `None` rather than a username.

- [ ] **Step 3: Rewrite `require_api_auth`**

In `admin_auth.py`, remove `ADMIN_TOKEN` from the `from .config import (...)` block (it's already gone per Task 3 — this just drops the now-broken import), and add `from . import access_control` to the imports. Replace `require_api_auth`:

```python
async def require_api_auth(request: Request) -> str:
    """FastAPI dependency for /api/* routes.

    Accepts an admin session cookie (browser) or a personal-token Bearer
    header (programmatic access). Does not accept MCP OAuth tokens — those
    are only valid for /mcp and /messages. Returns the authenticated
    username so callers can scope their query to it.
    """
    user = get_session_user(request)
    if user:
        return user
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        username = await access_control.validate_personal_token(auth[7:])
        if username:
            return username
    raise HTTPException(status_code=401, detail="Unauthorized")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_admin_auth.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add packages/aggregator/src/aggregator/admin_auth.py packages/aggregator/tests/test_admin_auth.py
git commit -m "feat(aggregator): require_api_auth resolves personal tokens and returns username"
```

---

## Task 6: `aggregator.py` — `ContextVar` + `/mcp` tool-list/dispatch filtering

**Files:**
- Modify: `packages/aggregator/src/aggregator/aggregator.py`

**Interfaces:**
- Consumes: `access_control.visible_server_names(username) -> set[str]` (Task 4); `meta_tools.call(name, arguments, username)` (signature changes in Task 8 — this task updates the call site to match; Task 8 lands the callee change).
- Produces: `aggregator.current_user: ContextVar[str]` — set by `main.py` (Task 7) before invoking `mcp_server.run`/`streamable_manager.handle_request`/`sse_transport.handle_post_message`; read by `handle_list_tools`/`handle_call_tool`.

No new test file for this task alone — its behavior is exercised by Task 14's `/mcp` integration test, since it only matters wired up through the real HTTP request path Task 7 provides. Verify with the existing suite (must not regress) and a manual smoke check.

- [ ] **Step 1: Edit `aggregator.py`**

```python
"""
MCP aggregator server.

Presents a single MCP endpoint that multiplexes tools from all running
child servers. Tools are namespaced as `<server>__<tool>` to avoid conflicts.
"""

from contextvars import ContextVar

from mcp import types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from . import access_control, meta_tools
from .child_manager import child_manager

# Set by main.py's bearer-auth check, once per /mcp request, before handing
# off to mcp_server.run()/streamable_manager.handle_request(). mcp_server
# below is one shared, stateless Server instance reused across every
# concurrent /mcp connection -- there's no per-connection object to hang
# per-request identity off of. A ContextVar is the right tool here (not a
# plain module variable): each request runs in its own asyncio task, and
# asyncio copies the context into new tasks, so this stays correctly
# isolated per concurrent connection even though mcp_server itself is
# long-lived and shared.
current_user: ContextVar[str] = ContextVar("current_user")


async def handle_list_tools(
    _ctx: ServerRequestContext, _params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    username = current_user.get()
    visible = await access_control.visible_server_names(username)
    tools = list(meta_tools.TOOLS)
    for server_name, tool in child_manager.all_tools():
        if server_name not in visible:
            continue
        tools.append(
            types.Tool(
                name=f"{server_name}__{tool.name}",
                description=f"[{server_name}] {tool.description or ''}".strip(),
                inputSchema=tool.input_schema,
            )
        )
    return types.ListToolsResult(tools=tools)


async def handle_call_tool(
    _ctx: ServerRequestContext, params: types.CallToolRequestParams
) -> types.CallToolResult:
    username = current_user.get()
    if params.name in meta_tools.NAMES:
        content = await meta_tools.call(params.name, params.arguments or {}, username)
        return types.CallToolResult(content=content, is_error=False)

    child, tool_name = child_manager.resolve(params.name)
    if child is None or not child.running:
        raise ValueError(f"No running server found for tool: {params.name!r}")
    if child.config.name not in await access_control.visible_server_names(username):
        raise ValueError(f"No running server found for tool: {params.name!r}")
    result = await child.session.call_tool(tool_name, params.arguments or {})
    return types.CallToolResult(content=result.content, is_error=result.is_error or False)


mcp_server = Server(
    "mcp-aggregator",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)
sse_transport = SseServerTransport("/messages/")

# Modern Streamable HTTP transport (single endpoint, POST-first), mounted
# alongside the legacy SSE transport above -- both wrap the same mcp_server,
# which is stateless/reentrant across concurrent connections by design.
streamable_manager = StreamableHTTPSessionManager(mcp_server, stateless=False)
```

- [ ] **Step 2: Confirm the module still imports (meta_tools.call's signature won't match yet — that's fixed by Task 8)**

Run: `cd packages/aggregator && uv run python -c "import aggregator.aggregator"`
Expected: succeeds (this only checks the import graph, not a live call — `meta_tools.call`'s old 2-arg signature still exists until Task 8, so calling it isn't exercised yet)

- [ ] **Step 3: Run the full existing suite to confirm no regressions so far**

Run: `cd packages/aggregator && uv run pytest -x`
Expected: some failures are EXPECTED at this point in the plan (test_routers.py/test_meta_tools.py still reference the old `ADMIN_TOKEN`/2-arg `call()` signature, fixed in Tasks 11-13) — confirm no *new* collection errors beyond those already-known pending fixes, and that Tasks 1-5's tests still pass.

- [ ] **Step 4: Commit**

```bash
git add packages/aggregator/src/aggregator/aggregator.py
git commit -m "feat(aggregator): scope /mcp tool list and dispatch to the requesting user"
```

---

## Task 7: `main.py` — retire `ADMIN_TOKEN`, resolve identity, `/api/me` + `/api/me/token`

**Files:**
- Modify: `packages/aggregator/src/aggregator/main.py`

**Interfaces:**
- Consumes: `oauth.validate_bearer(token) -> str | None` (existing); `access_control.validate_personal_token(token) -> str | None`, `access_control.generate_personal_token(username) -> str`, `access_control.is_admin(username) -> bool` (Task 4); `aggregator.current_user: ContextVar[str]` (Task 6).
- Produces: `_check_bearer(request) -> str` (was `-> None`); `GET /api/me` now returns `{"username", "is_admin"}`; new `POST /api/me/token` returns `{"token": str}`.

- [ ] **Step 1: Edit `main.py`**

Change the import block:

```python
from . import access_control, admin_auth, log_capture, oauth
from .aggregator import current_user, mcp_server, sse_transport, streamable_manager
from .api.oauth_router import router as oauth_router
from .api.routers import router as api_router
from .child_manager import child_manager
from .config import LOG_LEVEL, WEBUI_DIST_DIR
from .database import init_db, list_servers
```

(Drops `ADMIN_TOKEN` from the `config` import — it no longer exists — and adds `access_control`, `current_user`.)

Replace `_check_bearer`:

```python
async def _check_bearer(request: Request) -> str:
    """
    Bearer auth for /mcp and /messages.
    Accepts a valid OAuth access token or a personal token, and sets the
    resolved username on aggregator.current_user for handle_list_tools/
    handle_call_tool to read. Returns 401 + WWW-Authenticate so MCP clients
    can discover OAuth.

    Called two ways: as a FastAPI Depends() on the /mcp route, and directly
    (not via Depends()) from _messages_asgi below, since /messages is a raw
    ASGI Mount that bypasses FastAPI's dependency injection. Keep both call
    sites in mind if this signature ever needs another Depends()-injected
    parameter -- the Depends() site would keep compiling silently while the
    manual site would need updating too.
    """
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        username = await oauth.validate_bearer(token)
        if username is None:
            username = await access_control.validate_personal_token(token)
        if username:
            current_user.set(username)
            return username
    raise HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": oauth.www_authenticate_header()},
    )
```

Update the two `Depends(_check_bearer)` route signatures' unused-parameter type hints for consistency (functionally inert, just matches the new return type):

```python
@app.get("/mcp")
async def mcp_sse(request: Request, _: str = Depends(_check_bearer)):
```

```python
@app.post("/mcp")
async def mcp_streamable(request: Request, _: str = Depends(_check_bearer)):
```

`_messages_asgi`'s `await _check_bearer(request)` call site needs no change — it already ignores the return value, and the `ContextVar` side effect happens regardless.

Replace `/api/me` and add `/api/me/token` (near the existing "Admin auth routes" section):

```python
@app.get("/api/me")
async def api_me(request: Request):
    user = admin_auth.get_session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": user, "is_admin": access_control.is_admin(user)}


@app.post("/api/me/token")
async def api_generate_token(request: Request):
    user = admin_auth.get_session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = await access_control.generate_personal_token(user)
    return {"token": token}
```

(Both stay session-cookie-only, like `/api/me` already was — self-service token generation requires a browser login, matching the spec's chicken-and-egg reasoning: you need to already be logged in to mint the credential that replaces the login.)

- [ ] **Step 2: Manual smoke check (no automated test in this task — Task 14 covers it end-to-end)**

Run: `cd packages/aggregator && uv run python -c "import aggregator.main"`
Expected: succeeds

- [ ] **Step 3: Commit**

```bash
git add packages/aggregator/src/aggregator/main.py
git commit -m "feat(aggregator): retire ADMIN_TOKEN bearer auth, add personal-token endpoints"
```

---

## Task 8: `meta_tools.py` — username-scoped handlers, visibility field

**Files:**
- Modify: `packages/aggregator/src/aggregator/meta_tools.py`

**Interfaces:**
- Consumes: `access_control.visible_servers(username)`, `access_control.can_manage(server, username)` (Task 4).
- Produces: `meta_tools.call(name, arguments, username) -> list[types.TextContent]` (was `call(name, arguments)`); `_cfg(server)` now includes `"owner"` and `"visibility"` keys; `add_server`/`edit_server` tool schemas gain a `visibility` property.

No test changes in this task — Task 12 rewrites `test_meta_tools.py` to match this new signature and adds the scoping-specific tests. This task's own correctness is verified by getting the full suite green again in Task 12; keep this task's diff focused on `meta_tools.py` itself.

- [ ] **Step 1: Edit `meta_tools.py`**

Add to imports: `from . import access_control` and `from .models import Server, ServerType, ServerVisibility` (adds `ServerVisibility`).

Update `_cfg`:

```python
def _cfg(c: Server) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "type": c.type,
        "package": c.package,
        "args": c.get_args(),
        # Names only, not values -- this reaches an LLM's conversation
        # context over /mcp, not just the admin-only REST/webui surface.
        "env": dict.fromkeys(c.get_env(), "***"),
        "enabled": c.enabled,
        "owner": c.owner_username,
        "visibility": c.visibility,
    }
```

Replace `_find_by_name` (now requires management rights, not just existence):

```python
async def _find_by_name(name: str, username: str) -> Server:
    for server in await database.list_servers():
        if server.name == name and access_control.can_manage(server, username):
            return server
    raise ValueError(f"No server named {name!r}")
```

Replace `_list_servers` (scoped to visibility, not just existence):

```python
async def _list_servers(arguments: dict, username: str) -> list[dict]:
    servers = await access_control.visible_servers(username)
    running = {s["name"]: s for s in child_manager.status()}
    return [
        {
            **_cfg(s),
            "running": running.get(s.name, {}).get("running", False),
            "tool_count": running.get(s.name, {}).get("tool_count", 0),
            "error": running.get(s.name, {}).get("error"),
        }
        for s in servers
    ]
```

Replace `_add_server` (gains `username`, records ownership, adds `visibility`):

```python
async def _add_server(arguments: dict, username: str) -> dict:
    name = arguments["name"]
    type_str = arguments["type"]
    package = arguments["package"]
    args = arguments.get("args", [])
    env = arguments.get("env", {})
    visibility = arguments.get("visibility", ServerVisibility.PRIVATE.value)

    server_type = ServerType(type_str)  # raises ValueError for an unknown type
    ServerVisibility(visibility)  # raises ValueError for an unknown value

    try:
        config = await database.add_server(
            name,
            server_type,
            package,
            args,
            env,
            owner_username=username,
            visibility=visibility,
        )
    except Exception as exc:
        raise ValueError(str(exc)) from exc

    try:
        state = await child_manager.add(config)
        tools = [t.name for t in state.tools]
        error = None
    except Exception as exc:
        tools = []
        error = str(exc)

    return {"server": _cfg(config), "tools": tools, "error": error}
```

Replace `_edit_server`'s signature and its two internal calls that need `username` threaded through (`_find_by_name` and `database.update_server`); add `visibility` handling:

```python
async def _edit_server(arguments: dict, username: str) -> dict:
    name = arguments["name"]
    server = await _find_by_name(name, username)

    type_str = arguments.get("type")
    server_type = ServerType(type_str) if type_str is not None else None  # raises ValueError
    visibility = arguments.get("visibility")
    if visibility is not None:
        ServerVisibility(visibility)  # raises ValueError for an unknown value

    was_running = child_manager.get(server.name) is not None
    try:
        config = await database.update_server(
            server.id,
            name=arguments.get("new_name"),
            server_type=server_type,
            package=arguments.get("package"),
            args=arguments.get("args"),
            env=arguments.get("env"),
            visibility=visibility,
        )
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    if config is None:
        raise ValueError(f"No server named {name!r}")

    nothing_changed = (
        server.name == config.name
        and server.type == config.type
        and server.package == config.package
        and server.args == config.args
        and server.env == config.env
    )
    if nothing_changed:
        # A no-op edit shouldn't cycle a running child -- report its
        # current state as-is instead of restarting it for nothing. A
        # visibility-only change falls in here too (visibility doesn't
        # affect the running process): config already carries the new
        # value from update_server() above, so the returned _cfg(config)
        # reflects it correctly without a restart.
        state = child_manager.get(config.name)
        return {
            "server": _cfg(config),
            "tools": [t.name for t in state.tools] if state else [],
            "error": state.error if state else None,
        }

    renamed = server.name != config.name
    if was_running and (renamed or not config.enabled):
        await child_manager.remove(server.name)

    identity_changed = renamed or server.type != config.type or server.package != config.package
    if server.type == ServerType.GIT.value and identity_changed:
        await uninstall(server)

    if not config.enabled:
        return {"server": _cfg(config), "tools": [], "error": None}

    try:
        state = await child_manager.add(config)
        return {"server": _cfg(config), "tools": [t.name for t in state.tools], "error": None}
    except Exception as exc:
        return {"server": _cfg(config), "tools": [], "error": str(exc)}
```

Replace the remaining handlers (`_delete_server`, `_enable_server`, `_disable_server`, `_restart_server`) to accept and thread through `username`:

```python
async def _delete_server(arguments: dict, username: str) -> dict:
    name = arguments["name"]
    server = await _find_by_name(name, username)
    await child_manager.remove(server.name)
    await uninstall(server)
    await database.delete_server(server.id)
    return {"deleted": name}


async def _enable_server(arguments: dict, username: str) -> dict:
    name = arguments["name"]
    server = await _find_by_name(name, username)
    await database.update_server_enabled(server.id, True)
    server.enabled = True
    try:
        state = await child_manager.add(server)
        return {"name": name, "enabled": True, "tool_count": len(state.tools)}
    except Exception as exc:
        return {"name": name, "enabled": True, "tool_count": 0, "error": str(exc)}


async def _disable_server(arguments: dict, username: str) -> dict:
    name = arguments["name"]
    server = await _find_by_name(name, username)
    await child_manager.remove(server.name)
    await database.update_server_enabled(server.id, False)
    return {"name": name, "enabled": False}


async def _restart_server(arguments: dict, username: str) -> dict:
    name = arguments["name"]
    await _find_by_name(name, username)  # validates access with a clear error first
    try:
        state = await child_manager.restart(name)
    except KeyError as exc:
        raise ValueError(f"Server {name!r} is not running") from exc
    return {"name": name, "tool_count": len(state.tools)}
```

Add a `visibility` property to the `add_server` and `edit_server` tool schemas in the `TOOLS` list:

```python
    types.Tool(
        name="add_server",
        description=(f"Add and start a new MCP server ({'/'.join(t.value for t in ServerType)})."),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "type": {"type": "string"},
                "package": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}, "default": []},
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "default": {},
                },
                "visibility": {
                    "type": "string",
                    "enum": [v.value for v in ServerVisibility],
                    "default": ServerVisibility.PRIVATE.value,
                    "description": "Who can see/use this server: 'everyone' or 'private' (only you and admins).",
                },
            },
            "required": ["name", "type", "package"],
        },
    ),
    types.Tool(
        name="edit_server",
        description=(
            "Edit an existing MCP server's configuration. Identify the server "
            "with 'name'; only the other fields you provide are changed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Current name of the server to edit"},
                "new_name": {"type": "string", "description": "New name, if renaming"},
                "type": {"type": "string", "enum": [t.value for t in ServerType]},
                "package": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
                "visibility": {"type": "string", "enum": [v.value for v in ServerVisibility]},
            },
            "required": ["name"],
        },
    ),
```

Finally, replace `call()`:

```python
async def call(name: str, arguments: dict, username: str) -> list[types.TextContent]:
    result = await _HANDLERS[name](arguments, username)
    return [types.TextContent(type="text", text=json.dumps(result))]
```

- [ ] **Step 2: Confirm the module imports cleanly**

Run: `cd packages/aggregator && uv run python -c "import aggregator.meta_tools"`
Expected: succeeds

- [ ] **Step 3: Commit**

```bash
git add packages/aggregator/src/aggregator/meta_tools.py
git commit -m "feat(aggregator): scope meta-tools to the calling user, add visibility field"
```

(`test_meta_tools.py` is still on the old 2-arg `call()` signature at this point and will fail — that's Task 12, deliberately sequenced after this one.)

---

## Task 9: `routers.py` `/servers*` — username-scoped, visibility field, ownership 404s

**Files:**
- Modify: `packages/aggregator/src/aggregator/api/routers.py`

**Interfaces:**
- Consumes: `access_control.visible_servers(username)`, `access_control.can_manage(server, username)` (Task 4); `admin_auth.require_api_auth(request) -> str` (Task 5, already returns username via `Depends`).
- Produces: `AddServerRequest.visibility: ServerVisibility` (default `PRIVATE`); `ServerUpdateRequest.visibility: ServerVisibility | None`; every `/servers*` route now takes `username: str = Depends(require_api_auth)`.

- [ ] **Step 1: Edit `routers.py`**

Add to imports: `from .. import access_control` and `ServerVisibility` to the `from ..models import Server, ServerType` line (→ `from ..models import Server, ServerType, ServerVisibility`).

Update the request models:

```python
class AddServerRequest(BaseModel):
    name: str
    type: ServerType
    package: str
    args: list[str] = []
    env: dict[str, str] = {}
    visibility: ServerVisibility = ServerVisibility.PRIVATE


class ServerUpdateRequest(BaseModel):
    name: str | None = None
    type: ServerType | None = None
    package: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    visibility: ServerVisibility | None = None
```

Replace `api_list_servers`:

```python
@router.get("/servers")
async def api_list_servers(username: str = Depends(require_api_auth)):
    servers = await access_control.visible_servers(username)
    running = {s["name"]: s for s in child_manager.status()}
    return [
        {
            **_cfg(s),
            "running": running.get(s.name, {}).get("running", False),
            "tool_count": running.get(s.name, {}).get("tool_count", 0),
            "error": running.get(s.name, {}).get("error"),
        }
        for s in servers
    ]
```

Replace `api_add_server`:

```python
@router.post("/servers", status_code=201)
async def api_add_server(req: AddServerRequest, username: str = Depends(require_api_auth)):
    try:
        config = await add_server(
            req.name,
            req.type,
            req.package,
            req.args,
            req.env,
            owner_username=username,
            visibility=req.visibility.value,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        state = await child_manager.add(config)
        tools = [t.name for t in state.tools]
    except Exception as exc:
        tools = []
        return {"server": _cfg(config), "tools": tools, "error": str(exc)}

    return {"server": _cfg(config), "tools": tools}
```

Replace `api_update_server` (adds the `username` param, an ownership check, and passes `visibility` through):

```python
@router.patch("/servers/{server_id}")
async def api_update_server(
    server_id: int, req: ServerUpdateRequest, username: str = Depends(require_api_auth)
):
    existing = await get_server(server_id)
    if not existing or not access_control.can_manage(existing, username):
        raise HTTPException(status_code=404, detail="Server not found")

    was_running = child_manager.get(existing.name) is not None
    try:
        config = await update_server(
            server_id,
            name=req.name,
            server_type=req.type,
            package=req.package,
            args=req.args,
            env=req.env,
            visibility=req.visibility.value if req.visibility is not None else None,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if config is None:
        raise HTTPException(status_code=404, detail="Server not found")

    nothing_changed = (
        existing.name == config.name
        and existing.type == config.type
        and existing.package == config.package
        and existing.args == config.args
        and existing.env == config.env
    )
    if nothing_changed:
        state = child_manager.get(config.name)
        return {
            **_cfg(config),
            "running": state.running if state else False,
            "tool_count": len(state.tools) if state else 0,
            "error": state.error if state else None,
        }

    renamed = existing.name != config.name
    if was_running and (renamed or not config.enabled):
        await child_manager.remove(existing.name)

    identity_changed = renamed or existing.type != config.type or existing.package != config.package
    if existing.type == ServerType.GIT.value and identity_changed:
        await uninstall(existing)

    if not config.enabled:
        return {**_cfg(config), "running": False, "tool_count": 0, "error": None}

    try:
        state = await child_manager.add(config)
        return {**_cfg(config), "running": True, "tool_count": len(state.tools), "error": None}
    except Exception as exc:
        return {**_cfg(config), "running": False, "tool_count": 0, "error": str(exc)}
```

Replace `api_delete_server`, `api_enable_server`, `api_disable_server`, `api_restart_server` (each gains the `username` param and the same ownership check):

```python
@router.delete("/servers/{server_id}")
async def api_delete_server(server_id: int, username: str = Depends(require_api_auth)):
    config = await get_server(server_id)
    if not config or not access_control.can_manage(config, username):
        raise HTTPException(status_code=404, detail="Server not found")
    await child_manager.remove(config.name)
    await uninstall(config)
    await delete_server(server_id)
    return {"deleted": server_id}


@router.post("/servers/{server_id}/enable")
async def api_enable_server(server_id: int, username: str = Depends(require_api_auth)):
    config = await get_server(server_id)
    if not config or not access_control.can_manage(config, username):
        raise HTTPException(status_code=404, detail="Server not found")
    await update_server_enabled(server_id, True)
    config.enabled = True
    try:
        state = await child_manager.add(config)
        return {"id": server_id, "enabled": True, "tool_count": len(state.tools)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/servers/{server_id}/disable")
async def api_disable_server(server_id: int, username: str = Depends(require_api_auth)):
    config = await get_server(server_id)
    if not config or not access_control.can_manage(config, username):
        raise HTTPException(status_code=404, detail="Server not found")
    await child_manager.remove(config.name)
    await update_server_enabled(server_id, False)
    return {"id": server_id, "enabled": False}


@router.post("/servers/{server_id}/restart")
async def api_restart_server(server_id: int, username: str = Depends(require_api_auth)):
    config = await get_server(server_id)
    if not config or not access_control.can_manage(config, username):
        raise HTTPException(status_code=404, detail="Server not found")
    try:
        state = await child_manager.restart(config.name)
        return {"id": server_id, "tool_count": len(state.tools)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Server not running") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
```

Update the `_cfg` helper at the bottom of the file:

```python
def _cfg(c: Server) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "type": c.type,
        "package": c.package,
        "args": c.get_args(),
        "env": c.get_env(),
        "enabled": c.enabled,
        "owner": c.owner_username,
        "visibility": c.visibility,
    }
```

- [ ] **Step 2: Confirm the module imports cleanly**

Run: `cd packages/aggregator && uv run python -c "import aggregator.api.routers"`
Expected: succeeds

- [ ] **Step 3: Commit**

```bash
git add packages/aggregator/src/aggregator/api/routers.py
git commit -m "feat(aggregator): scope /api/servers routes to the calling user's access"
```

(`test_routers.py` is still on `ADMIN_TOKEN`-based auth at this point and will fail — Task 13 fixes it.)

---

## Task 10: `routers.py` `/tools`, `/tools/call`, `/logs*` — visibility scoping

Continuation of Task 9's file. Split out because it's a genuinely separate concern (usage access, not management) and the spec's self-review specifically flagged these three endpoints as an initially-missed gap — worth its own reviewable diff.

**Files:**
- Modify: `packages/aggregator/src/aggregator/api/routers.py`

**Interfaces:**
- Consumes: `access_control.visible_server_names(username)` (Task 4).
- Produces: `GET /tools`, `POST /tools/call`, `GET /logs`, `GET /logs/stream`, `GET /logs/{server_name}/stderr` all take `username: str = Depends(require_api_auth)` and scope their results/reject inaccessible servers with 404.

- [ ] **Step 1: Edit `routers.py`**

Replace `api_list_tools`:

```python
@router.get("/tools")
async def api_list_tools(username: str = Depends(require_api_auth)):
    visible = await access_control.visible_server_names(username)
    return [
        {
            "server": name,
            "tool": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
        }
        for name, tool in child_manager.all_tools()
        if name in visible
    ]
```

Replace `api_call_tool`:

```python
@router.post("/tools/call")
async def api_call_tool(req: CallToolRequest, username: str = Depends(require_api_auth)):
    if req.server not in await access_control.visible_server_names(username):
        raise HTTPException(status_code=404, detail=f"Server '{req.server}' not found")
    state = child_manager.get(req.server)
    if not state:
        raise HTTPException(status_code=404, detail=f"Server '{req.server}' not found")
    if not state.running:
        raise HTTPException(status_code=503, detail=f"Server '{req.server}' is not running")
    try:
        result = await state.session.call_tool(req.tool, req.arguments)
        content = [
            c.model_dump() if hasattr(c, "model_dump") else {"type": "unknown", "raw": str(c)}
            for c in result.content
        ]
        return {
            "server": req.server,
            "tool": req.tool,
            "content": content,
            "isError": result.is_error or False,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
```

Replace the `/logs*` routes:

```python
@router.get("/logs")
async def api_get_logs(
    server: str | None = None, limit: int = 200, username: str = Depends(require_api_auth)
):
    visible = await access_control.visible_server_names(username)
    if server:
        if server not in visible:
            raise HTTPException(status_code=404, detail=f"Server '{server}' not found")
        return log_capture.get_entries(server=server, limit=limit)
    return [
        e
        for e in log_capture.get_entries(limit=limit)
        if not e["server"] or e["server"] in visible
    ]


@router.get("/logs/stream")
async def api_stream_logs(
    request: Request, server: str | None = None, username: str = Depends(require_api_auth)
):
    from sse_starlette.sse import EventSourceResponse

    visible = await access_control.visible_server_names(username)
    if server and server not in visible:
        raise HTTPException(status_code=404, detail=f"Server '{server}' not found")

    async def generator():
        # log_capture._broker.subscribe(server=X) already only yields
        # entries for X when X is given, so the visibility check above
        # already fully scoped that case -- this per-entry filter only
        # matters for the server=None (all-servers) case.
        async for entry in log_capture._broker.subscribe(server=server):
            if server or not entry.server or entry.server in visible:
                yield {"data": _json.dumps(entry.as_dict())}

    return EventSourceResponse(generator())


@router.get("/logs/{server_name}/stderr")
async def api_get_stderr(
    server_name: str, limit: int = 200, username: str = Depends(require_api_auth)
):
    if server_name not in await access_control.visible_server_names(username):
        raise HTTPException(status_code=404, detail=f"Server '{server_name}' not found")
    lines = log_capture.read_log_file(server_name, limit=limit)
    return {"server": server_name, "lines": lines}
```

- [ ] **Step 2: Confirm the module imports cleanly**

Run: `cd packages/aggregator && uv run python -c "import aggregator.api.routers"`
Expected: succeeds

- [ ] **Step 3: Commit**

```bash
git add packages/aggregator/src/aggregator/api/routers.py
git commit -m "feat(aggregator): scope /api/tools and /api/logs routes to visible servers"
```

---

## Task 11: `conftest.py` — retire `ADMIN_TOKEN` fixture, add `ADMIN_USERS` + token factory

**Files:**
- Modify: `packages/aggregator/tests/conftest.py`

**Interfaces:**
- Produces: `token_for` fixture — `async def token_for(username: str) -> str`, a factory that mints a real personal token via `access_control.generate_personal_token`.

- [ ] **Step 1: Edit `conftest.py`**

Replace:

```python
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="aggregator-test-data-"))
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
```

with:

```python
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="aggregator-test-data-"))
os.environ.setdefault("ADMIN_USERS", "test-admin")
```

Add, after the existing `proxy_target_url` fixture:

```python
@pytest.fixture
async def token_for():
    """Factory fixture: `token = await token_for("alice")` mints a real
    personal token for that username via access_control, exercising the
    same hash-and-store path a real user's self-service token generation
    would."""
    from aggregator import access_control

    async def _make(username: str) -> str:
        return await access_control.generate_personal_token(username)

    return _make
```

- [ ] **Step 2: Run the tasks-1-through-4 tests to confirm `ADMIN_USERS=test-admin` now makes the previously-skipped assertions pass**

Run: `cd packages/aggregator && uv run pytest tests/test_access_control.py -v`
Expected: PASS (all 9, including the two that depended on `ADMIN_USERS` being set)

- [ ] **Step 3: Commit**

```bash
git add packages/aggregator/tests/conftest.py
git commit -m "test(aggregator): replace ADMIN_TOKEN fixture with ADMIN_USERS + token factory"
```

---

## Task 12: `test_meta_tools.py` — update to username-scoped `call()`, add scoping tests

**Files:**
- Modify: `packages/aggregator/tests/test_meta_tools.py`

**Interfaces:**
- Consumes: `meta_tools.call(name, arguments, username)` (Task 8); `token_for` fixture is not needed here — `meta_tools.call` takes a plain username string, no token/HTTP layer involved.

- [ ] **Step 1: Rewrite the file**

Replace the full contents of `packages/aggregator/tests/test_meta_tools.py`:

```python
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

USER = "meta-test-user"
OWNER = "meta-owner"
STRANGER = "meta-stranger"
ADMIN = "test-admin"  # set as ADMIN_USERS by conftest.py


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
                USER,
            )
        )
        assert added["error"] is None
        assert set(added["tools"]) == {"echo", "add"}

        listed = _payload(await meta_tools.call("list_servers", {}, USER))
        entry = next(s for s in listed if s["name"] == name)
        assert entry["running"] is True
        assert entry["tool_count"] == 2
        assert entry["error"] is None

        off = _payload(await meta_tools.call("disable_server", {"name": name}, USER))
        assert off == {"name": name, "enabled": False}
        assert child_manager.get(name) is None

        on = _payload(await meta_tools.call("enable_server", {"name": name}, USER))
        assert on["enabled"] is True
        assert on["tool_count"] == 2

        restarted = _payload(await meta_tools.call("restart_server", {"name": name}, USER))
        assert restarted == {"name": name, "tool_count": 2}

        deleted = _payload(await meta_tools.call("delete_server", {"name": name}, USER))
        assert deleted == {"deleted": name}

        listed_after = _payload(await meta_tools.call("list_servers", {}, USER))
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
            USER,
        )
        listed = _payload(await meta_tools.call("list_servers", {}, USER))
        entry = next(s for s in listed if s["name"] == name)
        assert entry["env"] == {"API_KEY": "***"}
    finally:
        await _cleanup_by_name(name)


async def test_action_on_unknown_server_name_raises_value_error():
    with pytest.raises(ValueError, match="No server named"):
        await meta_tools.call("restart_server", {"name": "does-not-exist"}, USER)


async def test_add_server_with_invalid_type_raises_value_error():
    with pytest.raises(ValueError):
        await meta_tools.call(
            "add_server",
            {"name": "x", "type": "not-a-real-type", "package": "y"},
            USER,
        )


async def test_edit_server_updates_only_provided_fields(proxy_target_url):
    name = "meta-edit-partial"
    try:
        await meta_tools.call(
            "add_server",
            {"name": name, "type": "proxy", "package": proxy_target_url, "env": {"A": "1"}},
            USER,
        )
        edited = _payload(
            await meta_tools.call("edit_server", {"name": name, "env": {"B": "2"}}, USER)
        )
        assert edited["error"] is None
        assert edited["server"]["package"] == proxy_target_url
        assert edited["server"]["env"] == {"B": "***"}
    finally:
        await _cleanup_by_name(name)


async def test_edit_server_rename_moves_child_manager_key(proxy_target_url):
    old_name, new_name = "meta-edit-rename-old", "meta-edit-rename-new"
    try:
        await meta_tools.call(
            "add_server", {"name": old_name, "type": "proxy", "package": proxy_target_url}, USER
        )
        assert child_manager.get(old_name) is not None

        edited = _payload(
            await meta_tools.call("edit_server", {"name": old_name, "new_name": new_name}, USER)
        )
        assert edited["server"]["name"] == new_name
        assert set(edited["tools"]) == {"echo", "add"}
        assert child_manager.get(old_name) is None
        assert child_manager.get(new_name) is not None
    finally:
        await _cleanup_by_name(old_name)
        await _cleanup_by_name(new_name)


async def test_edit_server_unknown_name_raises_value_error():
    with pytest.raises(ValueError, match="No server named"):
        await meta_tools.call("edit_server", {"name": "does-not-exist", "package": "x"}, USER)


async def test_edit_server_invalid_type_raises_value_error(proxy_target_url):
    name = "meta-edit-bad-type"
    try:
        await meta_tools.call(
            "add_server", {"name": name, "type": "proxy", "package": proxy_target_url}, USER
        )
        with pytest.raises(ValueError):
            await meta_tools.call("edit_server", {"name": name, "type": "not-a-real-type"}, USER)
    finally:
        await _cleanup_by_name(name)


async def test_edit_server_while_running_without_rename_restarts_in_place(proxy_target_url):
    name = "meta-edit-same-name"
    try:
        await meta_tools.call(
            "add_server", {"name": name, "type": "proxy", "package": proxy_target_url}, USER
        )
        assert child_manager.get(name) is not None

        edited = _payload(
            await meta_tools.call("edit_server", {"name": name, "env": {"X": "1"}}, USER)
        )
        assert edited["error"] is None
        assert set(edited["tools"]) == {"echo", "add"}
        assert edited["server"]["env"] == {"X": "***"}
        assert child_manager.get(name) is not None
    finally:
        await _cleanup_by_name(name)


async def test_edit_server_noop_does_not_restart_running_child(proxy_target_url):
    name = "meta-edit-noop"
    try:
        await meta_tools.call(
            "add_server", {"name": name, "type": "proxy", "package": proxy_target_url}, USER
        )
        original_session = child_manager.get(name).session

        edited = _payload(await meta_tools.call("edit_server", {"name": name}, USER))
        assert edited["error"] is None
        assert set(edited["tools"]) == {"echo", "add"}
        assert child_manager.get(name).session is original_session
    finally:
        await _cleanup_by_name(name)


async def test_edit_server_git_rename_uninstalls_old_checkout(monkeypatch):
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
            USER,
        )

        edited = _payload(
            await meta_tools.call("edit_server", {"name": old_name, "new_name": new_name}, USER)
        )
        assert edited["server"]["name"] == new_name

        assert len(calls) == 1
        assert calls[0].name == old_name
    finally:
        await _cleanup_by_name(old_name)
        await _cleanup_by_name(new_name)


async def test_edit_server_git_package_only_change_uninstalls_old_checkout(monkeypatch):
    name = "meta-edit-git-pkg-change"
    calls = []

    async def fake_uninstall(config):
        calls.append(config)

    monkeypatch.setattr("aggregator.meta_tools.uninstall", fake_uninstall)
    try:
        await meta_tools.call(
            "add_server",
            {"name": name, "type": "git", "package": "git+https://example.invalid/repo-a.git"},
            USER,
        )

        edited = _payload(
            await meta_tools.call(
                "edit_server",
                {"name": name, "package": "git+https://example.invalid/repo-b.git"},
                USER,
            )
        )
        assert edited["server"]["package"] == "git+https://example.invalid/repo-b.git"

        assert len(calls) == 1
        assert calls[0].name == name
        assert calls[0].package == "git+https://example.invalid/repo-a.git"
    finally:
        await _cleanup_by_name(name)


async def test_edit_server_git_to_non_git_type_change_uninstalls_old_checkout(monkeypatch):
    name = "meta-edit-git-type-change"
    calls = []

    async def fake_uninstall(config):
        calls.append(config)

    monkeypatch.setattr("aggregator.meta_tools.uninstall", fake_uninstall)
    try:
        await meta_tools.call(
            "add_server",
            {"name": name, "type": "git", "package": "git+https://example.invalid/repo.git"},
            USER,
        )

        edited = _payload(
            await meta_tools.call(
                "edit_server", {"name": name, "type": "cmd", "package": "/no/such/binary"}, USER
            )
        )
        assert edited["server"]["type"] == "cmd"

        assert len(calls) == 1
        assert calls[0].name == name
        assert calls[0].type == "git"
    finally:
        await _cleanup_by_name(name)


# ── Visibility / ownership scoping ────────────────────────────────────────────


async def test_add_server_defaults_to_private_visibility(proxy_target_url):
    name = "meta-visibility-default"
    try:
        added = _payload(
            await meta_tools.call(
                "add_server",
                {"name": name, "type": "proxy", "package": proxy_target_url},
                OWNER,
            )
        )
        assert added["server"]["visibility"] == "private"
        assert added["server"]["owner"] == OWNER
    finally:
        await _cleanup_by_name(name)


async def test_list_servers_hides_private_servers_from_other_users(proxy_target_url):
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
            OWNER,
        )
        owner_view = _payload(await meta_tools.call("list_servers", {}, OWNER))
        stranger_view = _payload(await meta_tools.call("list_servers", {}, STRANGER))
        assert any(s["name"] == name for s in owner_view)
        assert all(s["name"] != name for s in stranger_view)
    finally:
        await _cleanup_by_name(name)


async def test_list_servers_shows_everyone_visibility_to_all_users(proxy_target_url):
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
            OWNER,
        )
        stranger_view = _payload(await meta_tools.call("list_servers", {}, STRANGER))
        assert any(s["name"] == name for s in stranger_view)
    finally:
        await _cleanup_by_name(name)


async def test_stranger_cannot_manage_owners_private_server(proxy_target_url):
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
            OWNER,
        )
        with pytest.raises(ValueError, match="No server named"):
            await meta_tools.call("restart_server", {"name": name}, STRANGER)
        with pytest.raises(ValueError, match="No server named"):
            await meta_tools.call("delete_server", {"name": name}, STRANGER)
    finally:
        await _cleanup_by_name(name)


async def test_admin_can_manage_any_users_private_server(proxy_target_url):
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
            OWNER,
        )
        restarted = _payload(await meta_tools.call("restart_server", {"name": name}, ADMIN))
        assert restarted == {"name": name, "tool_count": 2}
    finally:
        await _cleanup_by_name(name)
```

- [ ] **Step 2: Run the tests**

Run: `cd packages/aggregator && uv run pytest tests/test_meta_tools.py -v`
Expected: PASS (all tests, old and new)

- [ ] **Step 3: Commit**

```bash
git add packages/aggregator/tests/test_meta_tools.py
git commit -m "test(aggregator): update meta_tools tests for username scoping, add visibility coverage"
```

---

## Task 13: `test_routers.py` — personal-token auth, visibility field, scoping tests

**Files:**
- Modify: `packages/aggregator/tests/test_routers.py`

**Interfaces:**
- Consumes: `token_for` fixture (Task 11); `require_api_auth` accepting personal tokens (Task 5); the scoped `/servers*`, `/tools*`, `/logs*` routes (Tasks 9-10).

- [ ] **Step 1: Rewrite the file**

Replace the full contents of `packages/aggregator/tests/test_routers.py`:

```python
"""
Regression tests for the /api/servers, /api/tools, and /api/logs REST
routes -- editing (PATCH /servers/{id}) plus per-user visibility/ownership
scoping added in docs/superpowers/plans/2026-08-20-per-user-server-access.md.

Uses a minimal FastAPI app (just api_router, no lifespan) over httpx's ASGI
transport. Auth is a real personal token minted via the token_for fixture
(conftest.py), exercising the same access_control.validate_personal_token
path a live personal token would.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aggregator.api.routers import router as api_router
from aggregator.child_manager import child_manager
from aggregator.database import delete_server, list_servers

OWNER = "router-owner"
STRANGER = "router-stranger"
ADMIN = "test-admin"  # set as ADMIN_USERS by conftest.py


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


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


async def test_add_server_defaults_to_private_and_records_owner(client, auth_headers):
    name = "router-visibility-default"
    try:
        added = await client.post(
            "/api/servers",
            json={"name": name, "type": "proxy", "package": "http://a.invalid/mcp"},
            headers=auth_headers,
        )
        body = added.json()["server"]
        assert body["visibility"] == "private"
        assert body["owner"] == OWNER
    finally:
        await _cleanup_by_name(name)


async def test_list_servers_hides_private_servers_from_other_users(client, auth_headers, stranger_headers):
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


async def test_list_servers_shows_everyone_visibility_to_all(client, auth_headers, stranger_headers):
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
            f"/api/servers/{server_id}", json={"package": "http://b.invalid/mcp"}, headers=stranger_headers
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
```

- [ ] **Step 2: Run the tests**

Run: `cd packages/aggregator && uv run pytest tests/test_routers.py -v`
Expected: PASS (all tests, old and new)

- [ ] **Step 3: Run the full suite**

Run: `cd packages/aggregator && uv run pytest`
Expected: PASS — every test in the aggregator package, including Tasks 1-12's

- [ ] **Step 4: Commit**

```bash
git add packages/aggregator/tests/test_routers.py
git commit -m "test(aggregator): update router tests for personal-token auth, add visibility coverage"
```

---

## Task 14: `/mcp` integration test — visibility filtering over a real HTTP connection

Verified interactively while writing this plan (uvicorn + `streamable_http_client` against the real `aggregator.main.app`, with a bearer token, successfully listed tools; a `ValueError` raised inside `handle_call_tool` surfaces to the client as `mcp.shared.exceptions.MCPError`) — this task transcribes that into a permanent regression test.

**Files:**
- Create: `packages/aggregator/tests/test_mcp_access_integration.py`

**Interfaces:**
- Consumes: `access_control.generate_personal_token` (Task 4); `aggregator.main.app` (Task 7); `child_manager.add`/`remove` (existing).

- [ ] **Step 1: Write the test**

Create `packages/aggregator/tests/test_mcp_access_integration.py`:

```python
"""
End-to-end regression test: two personal tokens hitting the real /mcp
endpoint over an actual HTTP connection see different tool lists, and a
stranger's direct tool-name call against a private server is rejected --
this is the behavior a mocked transport would likely miss, since it
exercises the aggregator.current_user ContextVar across a genuine
per-connection request handled by uvicorn, not a same-task direct call.
"""

import asyncio
import socket
import time

import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from aggregator import access_control
from aggregator.child_manager import child_manager
from aggregator.database import add_server, delete_server
from aggregator.main import app
from aggregator.models import ServerType, ServerVisibility


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"nothing listening on 127.0.0.1:{port} after {timeout}s")


@pytest.fixture
async def aggregator_url():
    """Run the real aggregator FastAPI app (with lifespan) on a free local
    port, so /mcp is exercised through a genuine per-connection HTTP
    request rather than a same-task direct function call."""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    await asyncio.to_thread(_wait_for_port, port)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await task


async def _list_tool_names(url: str, token: str) -> set[str]:
    async with streamable_http_client(
        url, http_client=httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return {t.name for t in result.tools}


async def test_mcp_tool_list_filters_private_servers_per_user(proxy_target_url, aggregator_url):
    owner_name = "mcp-integ-owner-private"
    shared_name = "mcp-integ-everyone"
    owner_token = await access_control.generate_personal_token("mcp-integ-owner")
    stranger_token = await access_control.generate_personal_token("mcp-integ-stranger")

    owner_config = await add_server(
        owner_name,
        ServerType.PROXY,
        proxy_target_url,
        owner_username="mcp-integ-owner",
        visibility=ServerVisibility.PRIVATE.value,
    )
    shared_config = await add_server(
        shared_name,
        ServerType.PROXY,
        proxy_target_url,
        owner_username="mcp-integ-owner",
        visibility=ServerVisibility.EVERYONE.value,
    )
    await child_manager.add(owner_config)
    await child_manager.add(shared_config)
    try:
        owner_tools = await _list_tool_names(aggregator_url, owner_token)
        stranger_tools = await _list_tool_names(aggregator_url, stranger_token)

        assert f"{owner_name}__echo" in owner_tools
        assert f"{shared_name}__echo" in owner_tools
        assert f"{owner_name}__echo" not in stranger_tools
        assert f"{shared_name}__echo" in stranger_tools
    finally:
        await child_manager.remove(owner_name)
        await child_manager.remove(shared_name)
        await delete_server(owner_config.id)
        await delete_server(shared_config.id)


async def test_mcp_call_tool_rejects_private_server_for_non_owner(proxy_target_url, aggregator_url):
    name = "mcp-integ-call-denied"
    owner_token = await access_control.generate_personal_token("mcp-integ-call-owner")
    stranger_token = await access_control.generate_personal_token("mcp-integ-call-stranger")

    config = await add_server(
        name,
        ServerType.PROXY,
        proxy_target_url,
        owner_username="mcp-integ-call-owner",
        visibility=ServerVisibility.PRIVATE.value,
    )
    await child_manager.add(config)
    try:
        async with streamable_http_client(
            aggregator_url,
            http_client=httpx2.AsyncClient(headers={"Authorization": f"Bearer {stranger_token}"}),
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                with pytest.raises(MCPError):
                    await session.call_tool(f"{name}__echo", {"text": "hi"})
    finally:
        await child_manager.remove(name)
        await delete_server(config.id)
```

- [ ] **Step 2: Run the test**

Run: `cd packages/aggregator && uv run pytest tests/test_mcp_access_integration.py -v`
Expected: PASS (2 passed). Uvicorn's access logging may print noisy `RuntimeError: Unexpected ASGI message ... after response already completed` tracebacks to stderr during the run — this is a pre-existing quirk of `mcp_streamable`'s double-send handling (documented in `main.py`'s own comments) unrelated to this feature, and does not fail the test.

- [ ] **Step 3: Run the full aggregator suite one more time**

Run: `cd packages/aggregator && uv run pytest`
Expected: PASS — full suite green

- [ ] **Step 4: Commit**

```bash
git add packages/aggregator/tests/test_mcp_access_integration.py
git commit -m "test(aggregator): add end-to-end /mcp visibility filtering integration test"
```

---

## Task 15: Docs & config — README, `.env.example`, `docker-compose.yml`, `scripts/init-env.sh`, `CLAUDE.md`

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `docker-compose.yml`
- Modify: `scripts/init-env.sh`
- Modify: `CLAUDE.md`

No test — documentation/config only. Verified by inspection in Step 2.

- [ ] **Step 1: Edit each file**

`.env.example` (`packages/../.env.example` at repo root) — replace the `ADMIN_TOKEN=` line:

```
ADMIN_USERS=
```

`docker-compose.yml` — replace:

```yaml
      ADMIN_TOKEN: "${ADMIN_TOKEN}"
```

with:

```yaml
      ADMIN_USERS: "${ADMIN_USERS}"
```

`scripts/init-env.sh` — four precise edits:

1. Remove line 12 entirely: `ADMIN_TOKEN=$(openssl rand -hex 32)`.
2. In the `if $INTERACTIVE; then` block, add a prompt after the `GITHUB_ALLOWED_USERS` one:
   ```bash
       read -rp "GitHub username(s) [comma-separated]: "    GITHUB_ALLOWED_USERS
       read -rp "Admin GitHub username(s) [comma-separated, optional]: " ADMIN_USERS
   ```
3. In the `else` (non-interactive) block, add:
   ```bash
       GITHUB_ALLOWED_USERS="CHANGE_ME"
       ADMIN_USERS=""
   ```
4. In the heredoc, replace:
   ```
   # ── Secret keys (auto-generated) ─────────────────────────────────────────────
   # Static bearer token for MCP clients (Claude Desktop etc.) — optional if OAuth only.
   ADMIN_TOKEN=${ADMIN_TOKEN}

   # Signing key for admin session cookies.
   SESSION_SECRET=${SESSION_SECRET}
   ```
   with:
   ```
   # ── Secret keys (auto-generated) ─────────────────────────────────────────────
   # Signing key for admin session cookies.
   SESSION_SECRET=${SESSION_SECRET}
   ```
   and add, right after the existing `GITHUB_ALLOWED_USERS=${GITHUB_ALLOWED_USERS}` line in the heredoc:
   ```

   # Comma-separated subset of GITHUB_ALLOWED_USERS with admin rights (see
   # and manage every server, override visibility). Optional — leave empty
   # for no admins.
   ADMIN_USERS=${ADMIN_USERS}
   ```

`CLAUDE.md` (repo root) — replace:

```
- Local non-Docker run: export `ADMIN_TOKEN` and `DATA_DIR` manually — the app doesn't load `.env` itself (only `docker-compose.yml` does), and `DATA_DIR` defaults to `/data` (unwritable outside Docker).
```

with:

```
- Local non-Docker run: export `DATA_DIR` manually (and `ADMIN_USERS` if you need admin rights locally) — the app doesn't load `.env` itself (only `docker-compose.yml` does), and `DATA_DIR` defaults to `/data` (unwritable outside Docker).
```

`README.md` — this is the largest set of edits; work through each:

1. Architecture diagram (line ~11): change `│  OAuth 2.1 + PKCE  OR  Bearer ADMIN_TOKEN` to `│  OAuth 2.1 + PKCE  OR  Bearer <personal token>`.
2. Auth model bullets (line ~29): change `or static \`ADMIN_TOKEN\` bearer token (Claude Desktop etc.)` to `or a personal API token (Claude Desktop etc. — generate one from the webui's Account page after logging in)`.
3. Local Testing section (lines ~90-99): replace the `TOKEN=$(grep ^ADMIN_TOKEN .env | cut -d= -f2)` example with a note that a personal token must be generated via the webui first (no `.env`-based shortcut exists anymore), e.g.:
   ```
   # 2. MCP bearer token access — generate a personal token first: log into
   #    the webui at http://localhost:8000/admin, open "Account", click
   #    "Generate token", then:
   TOKEN=<paste the generated token>
   curl -sI http://localhost:8000/mcp                                       # → 401
   curl -H "Authorization: Bearer $TOKEN" --max-time 2 http://localhost:8000/mcp  # → SSE stream
   ```
4. Claude Desktop config example (line ~128): change `"Authorization": "Bearer <ADMIN_TOKEN>"` to `"Authorization": "Bearer <your personal token>"`.
5. REST API section intro (line ~149): change `either a valid admin session cookie (browser) or \`Authorization: Bearer <ADMIN_TOKEN>\`` to `either a valid admin session cookie (browser) or \`Authorization: Bearer <personal token>\``.
6. The `curl` examples block (lines ~151-198): change `TOKEN=<ADMIN_TOKEN>` to `TOKEN=<your personal token>`, and add one example for the new endpoint after the existing "List servers" example:
   ```bash
   # Generate/regenerate your personal token (requires an active browser
   # session — cannot be done with a bearer token, only from a logged-in
   # webui session)
   curl -X POST -H "Authorization: Bearer $TOKEN" $BASE/api/me/token
   ```
   (Note inline in the README, right above this example, that this one specifically needs a session cookie, not a bearer token — the `curl` form here is illustrative of the endpoint's shape, not a literally-runnable bearer-token example for this one call.)
7. Add a `visibility` field to the "Add a server" `curl` example (line ~159-162):
   ```bash
   curl -X POST $BASE/api/servers \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"name":"<name>","type":"pypi|npm|git|cmd|proxy","package":"<package>","args":[],"env":{},"visibility":"private|everyone"}'
   ```
8. MCP Meta Tools table (lines ~205-213): add a `visibility?` note to `add_server`'s and `edit_server`'s Arguments column, e.g. `` `name`, `type`, `package`, `args?`, `env?`, `visibility?` `` for `add_server`.
9. The "Access model" callout right after the table (line ~217): this is now materially wrong (per-server access is exactly what this feature adds) — replace it with:
   ```
   > **Access model:** each server has an owner (whoever added it) and a
   > visibility (`everyone` or `private`). A caller can see and use
   > (`list_servers`, tool calls) any `everyone`-visibility server plus
   > their own `private` ones. Only the owner or an admin (`ADMIN_USERS`)
   > can manage a server (`edit_server`/`delete_server`/`enable_server`/
   > `disable_server`/`restart_server`). `env` values returned by
   > `list_servers` are redacted (`***`); variable names are visible but
   > not their values, since this output can land in an LLM's conversation
   > history rather than staying on the admin-only REST/web UI surface.
   ```
10. Environment Variables table (lines ~410-418): remove the `ADMIN_TOKEN` row, add:
    ```
    | `ADMIN_USERS` | — | Comma-separated GitHub usernames with admin rights (see all servers, manage any server, override visibility). Should be a subset of `GITHUB_ALLOWED_USERS`. |
    ```
11. Administration → Web UI bullet list (lines ~141-145): add a bullet:
    ```
    - **Account** — view your username/admin status, generate a personal API token for MCP clients (Claude Desktop etc.)
    ```

- [ ] **Step 2: Verify no stray `ADMIN_TOKEN` references remain**

Run: `grep -rn "ADMIN_TOKEN" README.md .env.example docker-compose.yml scripts/init-env.sh CLAUDE.md`
Expected: no output (empty grep result)

- [ ] **Step 3: Commit**

```bash
git add README.md .env.example docker-compose.yml scripts/init-env.sh CLAUDE.md
git commit -m "docs: document ADMIN_USERS, personal tokens, and server visibility"
```

---

## Task 16: Webui — extend types and API client

**Files:**
- Modify: `packages/webui/src/lib/types.ts`
- Modify: `packages/webui/src/lib/api.ts`

**Interfaces:**
- Produces: `ServerVisibility` type; `ServerConfig.owner`/`.visibility`; `AddServerInput.visibility`; `Me.is_admin`; `api.generateToken() -> Promise<{ token: string }>`.

- [ ] **Step 1: Edit `types.ts`**

```typescript
export type ServerType = "pypi" | "npm" | "git" | "cmd" | "proxy";
export type ServerVisibility = "everyone" | "private";

export interface ServerConfig {
  id: number;
  name: string;
  type: ServerType;
  package: string;
  args: string[];
  env: Record<string, string>;
  enabled: boolean;
  running: boolean;
  tool_count: number;
  error: string | null;
  owner: string | null;
  visibility: ServerVisibility;
}

export interface AddServerInput {
  name: string;
  type: ServerType;
  package: string;
  args: string[];
  env: Record<string, string>;
  visibility: ServerVisibility;
}

export interface AddServerResult {
  server: ServerConfig;
  tools: string[];
  error?: string;
}

export interface ToolInfo {
  server: string;
  tool: string;
  description: string | null;
  inputSchema: Record<string, unknown>;
}

export interface CallToolInput {
  server: string;
  tool: string;
  arguments: Record<string, unknown>;
}

export interface CallToolResult {
  server: string;
  tool: string;
  content: unknown[];
  isError: boolean;
}

export interface LogEntry {
  ts: number;
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR";
  server: string;
  msg: string;
}

export interface Me {
  username: string;
  is_admin: boolean;
}

export interface GenerateTokenResult {
  token: string;
}
```

- [ ] **Step 2: Edit `api.ts`**

Add `GenerateTokenResult` to the type import at the top, and add a method to the `api` object:

```typescript
import type {
  AddServerInput,
  AddServerResult,
  CallToolInput,
  CallToolResult,
  GenerateTokenResult,
  Me,
  ServerConfig,
  ToolInfo,
} from "./types";
```

```typescript
  generateToken: () =>
    request<GenerateTokenResult>("/api/me/token", { method: "POST" }),
```

(added inside the `export const api = { ... }` object, e.g. right after `me: () => request<Me>("/api/me"),`)

- [ ] **Step 3: Type-check**

Run: `cd packages/webui && pnpm build`
Expected: succeeds (this task alone doesn't yet use the new fields anywhere, so nothing should fail — it's here to confirm `types.ts`/`api.ts` themselves are syntactically valid before the next tasks consume them)

- [ ] **Step 4: Commit**

```bash
git add packages/webui/src/lib/types.ts packages/webui/src/lib/api.ts
git commit -m "feat(webui): add visibility/admin types and generateToken API call"
```

---

## Task 17: Webui — `AddServerDialog` visibility control

**Files:**
- Modify: `packages/webui/src/components/AddServerDialog.tsx`

**Interfaces:**
- Consumes: `ServerVisibility` type, `AddServerInput.visibility` (Task 16).

- [ ] **Step 1: Edit `AddServerDialog.tsx`**

Add `ServerVisibility` to the type import:

```typescript
import type { AddServerInput, ServerConfig, ServerType, ServerVisibility } from "@/lib/types";
```

Add visibility state, next to the existing `type`/`pkg` state:

```typescript
  const [visibility, setVisibility] = useState<ServerVisibility>("private");
```

In the prefill `useEffect`, set it from `server` (mirroring how `type` is prefilled):

```typescript
    setType(server?.type ?? "pypi");
    setVisibility(server?.visibility ?? "private");
```

In `handleSubmit`, include `visibility` in both branches (mirroring how `name`/`type`/`package` are always sent unconditionally for an edit, unlike the diffed `args`/`env`):

```typescript
    if (controlled && server) {
      const payload: Partial<AddServerInput> = { name, type, package: pkg, visibility };
      if (args !== initialArgs) payload.args = parseArgs(args);
      if (env !== initialEnv) payload.env = parseEnv(env);
      const result = await editServer.mutateAsync({ id: server.id, input: payload });
      if (result.error) return;
    } else {
      const payload = {
        name,
        type,
        package: pkg,
        args: parseArgs(args),
        env: parseEnv(env),
        visibility,
      };
      const result = await addServer.mutateAsync(payload);
      if (result.error) return;
    }
```

Add the control itself, as a third item in the existing `grid-cols-2` block that holds Name/Type (it wraps to a new row automatically with a third child):

```tsx
            <div className="space-y-1">
              <Label>Visibility</Label>
              <Select
                value={visibility}
                onValueChange={(v) => setVisibility(v as ServerVisibility)}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="private">Just me</SelectItem>
                  <SelectItem value="everyone">Everyone</SelectItem>
                </SelectContent>
              </Select>
            </div>
```

(placed as a sibling of the existing Name/Type `<div className="space-y-1">` blocks, inside the same `<div className="grid grid-cols-2 gap-4">`)

- [ ] **Step 2: Type-check**

Run: `cd packages/webui && pnpm build`
Expected: succeeds

- [ ] **Step 3: Manual verification**

Run: `just webui-dev` (with the backend also running per the README's dev instructions) and open `http://localhost:5173/admin`. Open "Add server", confirm the Visibility select appears and defaults to "Just me"; add a server, confirm it round-trips (edit it again, visibility reflects what was saved).

- [ ] **Step 4: Commit**

```bash
git add packages/webui/src/components/AddServerDialog.tsx
git commit -m "feat(webui): add visibility control to the add/edit server dialog"
```

---

## Task 18: Webui — `ServerTable` owner/visibility display + admin toggle

**Files:**
- Modify: `packages/webui/src/components/ServerTable.tsx`

**Interfaces:**
- Consumes: `ServerConfig.owner`/`.visibility` (Task 16); `useMe()` (existing); `useEditServer()` (existing).

- [ ] **Step 1: Edit `ServerTable.tsx`**

Add imports:

```typescript
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useMe } from "@/hooks/useMe";
import { useEditServer } from "@/hooks/useServers";
import type { ServerVisibility } from "@/lib/types";
```

Inside `ServerTable`, add:

```typescript
  const { data: me } = useMe();
  const editServer = useEditServer();
```

Add two `TableHead`s after "Package" and before "Status":

```tsx
            <TableHead>Owner</TableHead>
            <TableHead>Visibility</TableHead>
```

Add two `TableCell`s in the same position within the row-mapping body:

```tsx
              <TableCell className="text-muted-foreground">{s.owner ?? "—"}</TableCell>
              <TableCell>
                {me?.is_admin ? (
                  <Select
                    value={s.visibility}
                    onValueChange={(v) =>
                      editServer.mutate({
                        id: s.id,
                        input: { visibility: v as ServerVisibility },
                      })
                    }
                  >
                    <SelectTrigger size="sm">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="private">Private</SelectItem>
                      <SelectItem value="everyone">Everyone</SelectItem>
                    </SelectContent>
                  </Select>
                ) : (
                  <Badge variant={s.visibility === "everyone" ? "outline" : "secondary"}>
                    {s.visibility === "everyone" ? "Everyone" : "Private"}
                  </Badge>
                )}
              </TableCell>
```

- [ ] **Step 2: Type-check**

Run: `cd packages/webui && pnpm build`
Expected: succeeds

- [ ] **Step 3: Manual verification**

With the dev server running and logged in as a user in `ADMIN_USERS`: confirm the Owner/Visibility columns appear, and that changing the Visibility select on a row actually PATCHes and persists (reload the page, value sticks). Log in as a non-admin user (or temporarily unset `ADMIN_USERS`): confirm the same column now renders as a read-only badge, not a select.

- [ ] **Step 4: Commit**

```bash
git add packages/webui/src/components/ServerTable.tsx
git commit -m "feat(webui): show server owner/visibility, let admins toggle visibility inline"
```

---

## Task 19: Webui — Account page (token generation) + routing + nav

**Files:**
- Create: `packages/webui/src/hooks/useToken.ts`
- Create: `packages/webui/src/components/AccountPage.tsx`
- Modify: `packages/webui/src/router.tsx`
- Modify: `packages/webui/src/components/AppLayout.tsx`

**Interfaces:**
- Consumes: `api.generateToken()` (Task 16); `useMe()` (existing).
- Produces: `useGenerateToken()` hook; `AccountPage` component; `/account` route.

- [ ] **Step 1: Create `useToken.ts`**

Create `packages/webui/src/hooks/useToken.ts`:

```typescript
import { useMutation } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useGenerateToken() {
  return useMutation({ mutationFn: api.generateToken });
}
```

- [ ] **Step 2: Create `AccountPage.tsx`**

Create `packages/webui/src/components/AccountPage.tsx`:

```tsx
import { useState } from "react";
import { useMe } from "@/hooks/useMe";
import { useGenerateToken } from "@/hooks/useToken";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export function AccountPage() {
  const { data: me } = useMe();
  const generateToken = useGenerateToken();
  const [token, setToken] = useState<string | null>(null);

  return (
    <div className="max-w-lg space-y-6">
      <div>
        <h1 className="text-xl font-semibold">My account</h1>
        <div className="mt-2 flex items-center gap-2 text-sm text-muted-foreground">
          <span>{me?.username}</span>
          {me?.is_admin ? <Badge>Admin</Badge> : null}
        </div>
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
          </div>
        ) : null}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Wire the route**

In `router.tsx`, add the import:

```typescript
import { AccountPage } from "@/components/AccountPage";
```

Add the route definition (near `testerRoute`):

```typescript
export const accountRoute = createRoute({
  getParentRoute: () => authedLayoutRoute,
  path: "/account",
  component: AccountPage,
});
```

Add it to the tree:

```typescript
const routeTree = rootRoute.addChildren([
  loginRoute,
  authedLayoutRoute.addChildren([serversRoute, logsRoute, testerRoute, accountRoute]),
]);
```

- [ ] **Step 4: Add the nav link**

In `AppLayout.tsx`, add a `Link` after the existing "Tool Tester" link:

```tsx
            <Link
              to="/account"
              activeProps={{ className: "font-semibold text-foreground" }}
              className="text-sm text-muted-foreground"
            >
              Account
            </Link>
```

- [ ] **Step 5: Type-check**

Run: `cd packages/webui && pnpm build`
Expected: succeeds

- [ ] **Step 6: Manual verification**

With the dev server running: log in, click "Account" in the nav, click "Generate token", confirm a token appears; copy it and confirm it works as a Bearer token against `/api/servers` via `curl` (or against `/mcp` per the README's updated Local Testing instructions from Task 15).

- [ ] **Step 7: Commit**

```bash
git add packages/webui/src/hooks/useToken.ts packages/webui/src/components/AccountPage.tsx packages/webui/src/router.tsx packages/webui/src/components/AppLayout.tsx
git commit -m "feat(webui): add Account page with personal-token generation"
```

---

## Final verification

- [ ] Run the full backend suite: `cd packages/aggregator && uv run pytest -v`. Expected: all pass.
- [ ] Run lint: `just lint`. Expected: clean (fix anything `ruff check --fix`/`ruff format` would otherwise catch).
- [ ] Run the webui build: `cd packages/webui && pnpm build`. Expected: succeeds.
- [ ] Grep for any remaining `ADMIN_TOKEN` reference outside historical plan/spec docs: `grep -rn "ADMIN_TOKEN" --include="*.py" --include="*.ts" --include="*.tsx" --include="*.md" --include="*.yml" --include="*.sh" --include="*.example" . | grep -v docs/superpowers/plans | grep -v docs/superpowers/specs`. Expected: no output.
