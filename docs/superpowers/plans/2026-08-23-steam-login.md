# Steam Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Steam (OpenID 2.0) as a second, optional identity provider alongside GitHub, on both the admin webui session flow and the MCP client OAuth 2.1 + PKCE flow.

**Architecture:** A new `identity_providers.py` module defines a small `IdentityProvider` protocol (`login_redirect`/`resolve_callback`) with `GitHubProvider` (existing OAuth 2.0 logic, moved and prefixed) and `SteamProvider` (new OpenID 2.0 flow) implementations. Both `admin_auth.py`'s browser-session flow and `oauth.py`/`api/oauth_router.py`'s MCP PKCE flow are refactored to call this shared abstraction instead of hardcoding GitHub, so the CSRF/session/token-issuing plumbing (already provider-agnostic internally) isn't duplicated.

**Tech Stack:** FastAPI, httpx (OpenID/OAuth HTTP calls), itsdangerous (signed cookies), SQLModel/SQLite, React/TanStack Query/Router (webui).

**Spec:** `docs/superpowers/specs/2026-08-23-steam-login-design.md`

## Global Constraints

- Identity is prefixed and never linked across providers: `"github:octocat"`, `"steam:76561198012345678"`. `Server.owner_username`, `PersonalToken.username`, `OAuthToken.username`, and `ADMIN_USERS` all hold these prefixed strings going forward.
- `GITHUB_ALLOWED_USERS` and the new `STEAM_ALLOWED_USERS` stay **unprefixed** (raw GitHub logins / raw SteamID64s) — existing `.env` files for `GITHUB_ALLOWED_USERS` need no changes. `ADMIN_USERS` values must be prefixed.
- Both `GITHUB_CLIENT_ID`+`GITHUB_CLIENT_SECRET` and `STEAM_API_KEY` are optional; at least one must be configured or the app refuses to start.
- `STEAM_API_KEY`'s presence is the sole enable flag for Steam login — no separate toggle.
- GitHub's existing OAuth App callback URL (`/oauth/callback`) does not change.
- The `check_authentication` POST back to Steam is mandatory and is the actual security boundary for Steam login — never skip it or treat a callback as valid without it.
- A caller without access gets treated as "authentication failed", not a distinguishable "you exist but aren't allowed" message, matching this project's established access-control error style.
- No account linking, no new "User" table — every existing identity-shaped column/env var already holds plain strings and keeps doing so.
- Every `uv run`/`uvx` command runs from `packages/aggregator/`. Frontend commands run from `packages/webui/` (`pnpm build`, `pnpm lint`).

---

## Task 1: Data model — `OAuthToken.username` rename + migration

**Files:**
- Modify: `packages/aggregator/src/aggregator/models.py`
- Modify: `packages/aggregator/src/aggregator/database.py`
- Test: `packages/aggregator/tests/test_database.py`

**Interfaces:**
- Produces: `models.OAuthToken.username: str` (was `github_user`); `database._migrate_oauth_tokens_table(conn: AsyncConnection) -> None`.

- [ ] **Step 1: Write the failing test**

Add to `packages/aggregator/tests/test_database.py` (add `from aggregator.database import _migrate_oauth_tokens_table` to the existing import block from `aggregator.database`):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_database.py -v -k migrate_oauth_tokens`
Expected: FAIL — `ImportError: cannot import name '_migrate_oauth_tokens_table'`

- [ ] **Step 3: Rename the model field**

In `packages/aggregator/src/aggregator/models.py`, change:

```python
class OAuthToken(SQLModel, table=True):
    __tablename__ = "oauth_tokens"

    token: str = Field(primary_key=True)
    token_type: str
    username: str
    client_id: str
    expires_at: float
    created_at: float = Field(default_factory=_time.time)
```

(only `github_user: str` → `username: str` changes on this class)

- [ ] **Step 4: Add the migration function**

In `packages/aggregator/src/aggregator/database.py`, add above `init_db()`:

```python
async def _migrate_oauth_tokens_table(conn: AsyncConnection) -> None:
    """oauth_tokens.github_user was renamed to `username` when Steam login
    was added (it now holds a prefixed identity like "github:octocat" or
    "steam:76561198012345678", not just a GitHub login). These rows are
    short-lived session/refresh tokens (<=30 day TTL, already pruned by
    oauth.cleanup_expired()) -- not durable data worth writing a real
    column-rename migration for. On upgrade, a legacy-shaped table is
    dropped here, *before* create_all() runs in init_db(), so create_all()
    sees no table and recreates it fresh with the new schema. Any MCP
    client with an active session at upgrade time simply redoes OAuth once.
    """

    def _sync(sync_conn):
        inspector = inspect(sync_conn)
        if "oauth_tokens" not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns("oauth_tokens")}
        if "github_user" in existing and "username" not in existing:
            sync_conn.execute(text("DROP TABLE oauth_tokens"))

    await conn.run_sync(_sync)
```

Change `init_db()` — **the migration must run before `create_all`**, not after (unlike `_migrate_server_columns`, which alters an existing table and so must run after `create_all` has ensured the table exists; this one drops a table so `create_all` can recreate it fresh):

```python
async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with _engine.begin() as conn:
        await _migrate_oauth_tokens_table(conn)
        await conn.run_sync(SQLModel.metadata.create_all)
        await _migrate_server_columns(conn)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_database.py -v`
Expected: PASS (all tests, old and new)

- [ ] **Step 6: Commit**

```bash
git add packages/aggregator/src/aggregator/models.py packages/aggregator/src/aggregator/database.py packages/aggregator/tests/test_database.py
git commit -m "feat(aggregator): rename OAuthToken.github_user to username for multi-provider identity"
```

---

## Task 2: Config — `STEAM_API_KEY`, `STEAM_ALLOWED_USERS`

**Files:**
- Modify: `packages/aggregator/src/aggregator/config.py`

**Interfaces:**
- Produces: `config.STEAM_API_KEY: str`; `config.STEAM_ALLOWED_USERS: set[str]`.

No dedicated test file — a two-line config addition, exercised by later tasks' tests.

- [ ] **Step 1: Edit `config.py`**

Add after the existing `GITHUB_ALLOWED_USERS`/`ADMIN_USERS` block:

```python
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
STEAM_ALLOWED_USERS: set[str] = {
    u.strip() for u in os.getenv("STEAM_ALLOWED_USERS", "").split(",") if u.strip()
}
```

- [ ] **Step 2: Verify the module still imports**

Run: `cd packages/aggregator && uv run python -c "from aggregator import config; print(config.STEAM_API_KEY, config.STEAM_ALLOWED_USERS)"`
Expected: prints `set()` and an empty string (neither env var set in this shell)

- [ ] **Step 3: Commit**

```bash
git add packages/aggregator/src/aggregator/config.py
git commit -m "feat(aggregator): add STEAM_API_KEY and STEAM_ALLOWED_USERS config"
```

---

## Task 3: `access_control.is_allowed` — provider-aware allowlist dispatch

**Files:**
- Modify: `packages/aggregator/src/aggregator/access_control.py`
- Test: `packages/aggregator/tests/test_access_control.py`

**Interfaces:**
- Consumes: `config.GITHUB_ALLOWED_USERS`, `config.STEAM_ALLOWED_USERS` (Task 2).
- Produces: `access_control.is_allowed(username: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

Add to `packages/aggregator/tests/test_access_control.py` (add `is_allowed` to the existing `from aggregator import access_control` usage — no new import needed, `access_control.is_allowed` is called via the module):

```python
def test_is_allowed_true_for_github_user_in_allowlist(monkeypatch):
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", {"octocat"})
    assert access_control.is_allowed("github:octocat")


def test_is_allowed_false_for_github_user_not_in_allowlist(monkeypatch):
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", {"octocat"})
    assert not access_control.is_allowed("github:someone-else")


def test_is_allowed_true_for_github_user_when_allowlist_empty(monkeypatch):
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", set())
    assert access_control.is_allowed("github:anyone")


def test_is_allowed_true_for_steam_user_in_allowlist(monkeypatch):
    monkeypatch.setattr(access_control, "STEAM_ALLOWED_USERS", {"76561198012345678"})
    assert access_control.is_allowed("steam:76561198012345678")


def test_is_allowed_false_for_steam_user_not_in_allowlist(monkeypatch):
    monkeypatch.setattr(access_control, "STEAM_ALLOWED_USERS", {"76561198012345678"})
    assert not access_control.is_allowed("steam:99999999999999999")


def test_is_allowed_false_for_unknown_provider_prefix():
    assert not access_control.is_allowed("discord:someone")


def test_is_allowed_false_for_unprefixed_username():
    assert not access_control.is_allowed("octocat")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_access_control.py -v -k is_allowed`
Expected: FAIL — `AttributeError: module 'aggregator.access_control' has no attribute 'is_allowed'`

- [ ] **Step 3: Implement `is_allowed` and use it in `validate_personal_token`**

In `packages/aggregator/src/aggregator/access_control.py`, change the import line:

```python
from .config import ADMIN_USERS, GITHUB_ALLOWED_USERS, STEAM_ALLOWED_USERS
```

Update the module docstring to mention this new responsibility:

```python
"""
Single source of truth for who can see and manage which MCP servers, for
personal API token hashing/validation, and for whether a resolved identity
(from any identity provider) is allowed to authenticate at all. Imported by
the REST API (routers.py), the MCP meta-tools (meta_tools.py), the /mcp
tool-list/dispatch handlers (aggregator.py), and the auth flows
(admin_auth.py, oauth.py) -- the rule lives here exactly once.
"""
```

Add, near the top (after imports, before `is_admin`):

```python
def is_allowed(username: str) -> bool:
    """True if a prefixed identity (e.g. "github:octocat",
    "steam:76561198012345678") is allowed to authenticate, per the
    matching provider's allowlist. Empty allowlist = unrestricted for that
    provider. An unrecognized provider prefix is never allowed."""
    provider, sep, raw = username.partition(":")
    if not sep:
        return False
    if provider == "github":
        return not GITHUB_ALLOWED_USERS or raw in GITHUB_ALLOWED_USERS
    if provider == "steam":
        return not STEAM_ALLOWED_USERS or raw in STEAM_ALLOWED_USERS
    return False
```

Change `validate_personal_token`:

```python
async def validate_personal_token(token: str) -> str | None:
    username = await get_username_by_token_hash(_hash_token(token))
    if username and not is_allowed(username):
        return None
    return username
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_access_control.py -v`
Expected: PASS (all tests, old and new)

- [ ] **Step 5: Commit**

```bash
git add packages/aggregator/src/aggregator/access_control.py packages/aggregator/tests/test_access_control.py
git commit -m "feat(aggregator): add access_control.is_allowed for provider-aware allowlisting"
```

---

## Task 4: `identity_providers.py` — protocol + `GitHubProvider`

**Files:**
- Create: `packages/aggregator/src/aggregator/identity_providers.py`
- Test: `packages/aggregator/tests/test_identity_providers.py` (new)

**Interfaces:**
- Consumes: `config.GITHUB_CLIENT_ID`, `config.GITHUB_CLIENT_SECRET`, `config.MCP_DOMAIN`.
- Produces: `identity_providers.ProviderResult` (dataclass: `username: str`, `display_name: str`); `identity_providers.IdentityProvider` (Protocol: `slug: str`, `is_configured() -> bool`, `login_redirect(state: str) -> RedirectResponse`, `async resolve_callback(request: Request) -> ProviderResult | None`); `identity_providers.GitHubProvider`; `identity_providers.github_provider` (module-level singleton instance).

- [ ] **Step 1: Write the failing tests**

Create `packages/aggregator/tests/test_identity_providers.py`:

```python
"""
Tests for identity_providers.py -- the IdentityProvider abstraction shared
by admin_auth.py's browser-session flow and oauth.py's MCP PKCE flow.

There is no local GitHub/Steam to talk to, so these tests mock the
outbound httpx calls -- the one place in this project's test suite where
mocking the transport is the right tool rather than a shortcut, since the
whole point is verifying behavior against a *third-party* service's HTTP
contract, not our own code's transport handling.
"""

import httpx
import pytest
from fastapi import Request

from aggregator import identity_providers


def _request_with_query(query_string: str) -> Request:
    return Request(
        {
            "type": "http",
            "query_string": query_string.encode(),
            "headers": [],
        }
    )


def test_github_provider_is_configured_true_when_both_set(monkeypatch):
    monkeypatch.setattr(identity_providers, "GITHUB_CLIENT_ID", "id")
    monkeypatch.setattr(identity_providers, "GITHUB_CLIENT_SECRET", "secret")
    assert identity_providers.github_provider.is_configured()


def test_github_provider_is_configured_false_when_either_missing(monkeypatch):
    monkeypatch.setattr(identity_providers, "GITHUB_CLIENT_ID", "")
    monkeypatch.setattr(identity_providers, "GITHUB_CLIENT_SECRET", "secret")
    assert not identity_providers.github_provider.is_configured()


def test_github_provider_login_redirect_targets_github_with_state():
    response = identity_providers.github_provider.login_redirect("my-state-value")
    assert response.status_code == 302
    assert "github.com/login/oauth/authorize" in response.headers["location"]
    assert "state=my-state-value" in response.headers["location"]


async def test_github_provider_resolve_callback_returns_prefixed_result(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "gh-token"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octocat"})
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(
        identity_providers,
        "_github_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0),
    )

    result = await identity_providers.github_provider.resolve_callback(
        _request_with_query("code=abc123&state=xyz")
    )
    assert result == identity_providers.ProviderResult(
        username="github:octocat", display_name="octocat"
    )


async def test_github_provider_resolve_callback_returns_none_on_error_param():
    result = await identity_providers.github_provider.resolve_callback(
        _request_with_query("error=access_denied")
    )
    assert result is None


async def test_github_provider_resolve_callback_returns_none_on_missing_code():
    result = await identity_providers.github_provider.resolve_callback(_request_with_query(""))
    assert result is None


async def test_github_provider_resolve_callback_returns_none_when_no_access_token(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        identity_providers,
        "_github_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0),
    )
    result = await identity_providers.github_provider.resolve_callback(
        _request_with_query("code=abc123&state=xyz")
    )
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_identity_providers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'aggregator.identity_providers'`

- [ ] **Step 3: Implement `identity_providers.py`**

Create `packages/aggregator/src/aggregator/identity_providers.py`:

```python
"""
Identity providers for both auth surfaces: admin_auth.py's browser-session
flow and oauth.py's MCP OAuth 2.1 + PKCE flow. Each provider resolves a
callback request to a prefixed identity string ("github:octocat",
"steam:76561198012345678") -- callers never see provider-specific request
shapes, and neither provider knows about allowlists (see
access_control.is_allowed).
"""

import logging
import urllib.parse
from dataclasses import dataclass
from typing import Protocol

import httpx
from fastapi import Request
from fastapi.responses import RedirectResponse

from .config import GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, MCP_DOMAIN

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderResult:
    username: str  # prefixed identity, e.g. "github:octocat"
    display_name: str  # persona name (Steam) or login (GitHub, same as username's suffix)


class IdentityProvider(Protocol):
    slug: str

    def is_configured(self) -> bool: ...
    def login_redirect(self, state: str) -> RedirectResponse: ...
    async def resolve_callback(self, request: Request) -> ProviderResult | None: ...


def _github_http_client() -> httpx.AsyncClient:
    """Factory, not a module-level client -- tests monkeypatch this to
    inject a mocked transport without touching real network."""
    return httpx.AsyncClient(timeout=10.0)


class GitHubProvider:
    slug = "github"

    def is_configured(self) -> bool:
        return bool(GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET)

    def login_redirect(self, state: str) -> RedirectResponse:
        params = urllib.parse.urlencode(
            {
                "client_id": GITHUB_CLIENT_ID,
                "redirect_uri": f"https://{MCP_DOMAIN}/oauth/callback",
                "scope": "read:user",
                "state": state,
            }
        )
        return RedirectResponse(
            f"https://github.com/login/oauth/authorize?{params}", status_code=302
        )

    async def resolve_callback(self, request: Request) -> ProviderResult | None:
        code = request.query_params.get("code")
        error = request.query_params.get("error")
        if error or not code:
            logger.warning("GitHub callback error: %s", error or "missing code")
            return None
        try:
            async with _github_http_client() as h:
                token_resp = await h.post(
                    "https://github.com/login/oauth/access_token",
                    data={
                        "client_id": GITHUB_CLIENT_ID,
                        "client_secret": GITHUB_CLIENT_SECRET,
                        "code": code,
                    },
                    headers={"Accept": "application/json"},
                )
                token_resp.raise_for_status()
                gh_token = token_resp.json().get("access_token")
                if not gh_token:
                    logger.warning("GitHub returned no access_token: %s", token_resp.json())
                    return None
                user_resp = await h.get(
                    "https://api.github.com/user",
                    headers={
                        "Authorization": f"Bearer {gh_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                user_resp.raise_for_status()
                login: str = user_resp.json().get("login", "")
        except Exception as exc:
            logger.warning("GitHub exchange error: %s", exc)
            return None
        if not login:
            return None
        return ProviderResult(username=f"github:{login}", display_name=login)


github_provider = GitHubProvider()
```

Note: `token_resp = await h.post("https://github.com/login/oauth/access_token", ...)` and `h.get("https://api.github.com/user", ...)` use *absolute* URLs even though `h` is an `httpx.AsyncClient()` with no `base_url` — this is deliberate and matches the original code in `admin_auth.py`/`oauth.py` being replaced; `httpx.AsyncClient` accepts full URLs on any request regardless of `base_url`. The mocked test's `httpx.MockTransport(handler)` intercepts by URL path regardless of host, so this works correctly with the test's `handler` matching on `request.url.path`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_identity_providers.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add packages/aggregator/src/aggregator/identity_providers.py packages/aggregator/tests/test_identity_providers.py
git commit -m "feat(aggregator): add identity_providers module with IdentityProvider protocol and GitHubProvider"
```

---

## Task 5: `identity_providers.py` — `SteamProvider` + registry

**Files:**
- Modify: `packages/aggregator/src/aggregator/identity_providers.py`
- Test: `packages/aggregator/tests/test_identity_providers.py`

**Interfaces:**
- Consumes: `config.STEAM_API_KEY`, `config.MCP_DOMAIN`.
- Produces: `identity_providers.SteamProvider`; `identity_providers.steam_provider`; `identity_providers.PROVIDERS: dict[str, IdentityProvider]`; `identity_providers.configured_providers() -> list[IdentityProvider]`; `identity_providers.get_provider(slug: str) -> IdentityProvider | None`.

- [ ] **Step 1: Write the failing tests**

Append to `packages/aggregator/tests/test_identity_providers.py`:

```python
def test_steam_provider_is_configured_true_when_api_key_set(monkeypatch):
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "some-key")
    assert identity_providers.steam_provider.is_configured()


def test_steam_provider_is_configured_false_when_api_key_unset(monkeypatch):
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "")
    assert not identity_providers.steam_provider.is_configured()


def test_steam_provider_login_redirect_targets_steam_with_state_in_return_to():
    response = identity_providers.steam_provider.login_redirect("my-state-value")
    assert response.status_code == 302
    location = response.headers["location"]
    assert "steamcommunity.com/openid/login" in location
    assert "openid.mode=checkid_setup" in location
    # the state travels inside the (urlencoded) openid.return_to URL, not
    # as its own top-level query param -- decode return_to and check there
    parsed = urllib.parse.urlparse(location)
    qs = urllib.parse.parse_qs(parsed.query)
    return_to = qs["openid.return_to"][0]
    assert "state=my-state-value" in urllib.parse.urlparse(return_to).query


async def test_steam_provider_resolve_callback_accepts_valid_response(monkeypatch):
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "steamcommunity.com":
            assert b"openid.mode=check_authentication" in request.content
            return httpx.Response(200, text="ns:http://specs.openid.net/auth/2.0\nis_valid:true\n")
        if request.url.host == "api.steampowered.com":
            return httpx.Response(
                200,
                json={"response": {"players": [{"personaname": "CoolGamer99"}]}},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(
        identity_providers,
        "_steam_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0),
    )

    result = await identity_providers.steam_provider.resolve_callback(
        _request_with_query(
            "openid.mode=id_res&openid.claimed_id="
            "https%3A%2F%2Fsteamcommunity.com%2Fopenid%2Fid%2F76561198012345678"
            "&openid.sig=abc"
        )
    )
    assert result == identity_providers.ProviderResult(
        username="steam:76561198012345678", display_name="CoolGamer99"
    )


async def test_steam_provider_resolve_callback_rejects_forged_response(monkeypatch):
    """The security-critical case: a callback with valid-looking openid.*
    params but a check_authentication response of is_valid:false must be
    rejected -- this is what stops an attacker from forging a callback
    claiming an arbitrary SteamID."""
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ns:http://specs.openid.net/auth/2.0\nis_valid:false\n")

    monkeypatch.setattr(
        identity_providers,
        "_steam_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0),
    )

    result = await identity_providers.steam_provider.resolve_callback(
        _request_with_query(
            "openid.mode=id_res&openid.claimed_id="
            "https%3A%2F%2Fsteamcommunity.com%2Fopenid%2Fid%2F76561198012345678"
            "&openid.sig=forged"
        )
    )
    assert result is None


async def test_steam_provider_resolve_callback_rejects_wrong_mode():
    result = await identity_providers.steam_provider.resolve_callback(
        _request_with_query("openid.mode=cancel")
    )
    assert result is None


async def test_steam_provider_resolve_callback_falls_back_to_steamid_without_api_key(monkeypatch):
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "steamcommunity.com"
        return httpx.Response(200, text="ns:http://specs.openid.net/auth/2.0\nis_valid:true\n")

    monkeypatch.setattr(
        identity_providers,
        "_steam_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0),
    )

    result = await identity_providers.steam_provider.resolve_callback(
        _request_with_query(
            "openid.mode=id_res&openid.claimed_id="
            "https%3A%2F%2Fsteamcommunity.com%2Fopenid%2Fid%2F76561198012345678"
            "&openid.sig=abc"
        )
    )
    assert result == identity_providers.ProviderResult(
        username="steam:76561198012345678", display_name="76561198012345678"
    )


def test_configured_providers_reflects_which_are_set(monkeypatch):
    monkeypatch.setattr(identity_providers, "GITHUB_CLIENT_ID", "id")
    monkeypatch.setattr(identity_providers, "GITHUB_CLIENT_SECRET", "secret")
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "")
    assert identity_providers.configured_providers() == [identity_providers.github_provider]


def test_get_provider_returns_matching_provider_or_none():
    assert identity_providers.get_provider("github") is identity_providers.github_provider
    assert identity_providers.get_provider("steam") is identity_providers.steam_provider
    assert identity_providers.get_provider("discord") is None
```

Add `import urllib.parse` is already present at module level from Task 4; the test file needs it too — add `import urllib.parse` to this test file's imports at the top.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_identity_providers.py -v -k steam or configured_providers or get_provider`
Expected: FAIL — `AttributeError: module 'aggregator.identity_providers' has no attribute 'STEAM_API_KEY'` (and similar)

- [ ] **Step 3: Implement `SteamProvider` and the registry**

Add to `packages/aggregator/src/aggregator/identity_providers.py`. First, change the config import line to include `STEAM_API_KEY`:

```python
from .config import GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, MCP_DOMAIN, STEAM_API_KEY
```

Append (after `github_provider = GitHubProvider()`):

```python
STEAM_OPENID_ENDPOINT = "https://steamcommunity.com/openid/login"
STEAM_CLAIMED_ID_PREFIX = "https://steamcommunity.com/openid/id/"


def _steam_http_client() -> httpx.AsyncClient:
    """Factory, not a module-level client -- tests monkeypatch this to
    inject a mocked transport without touching real network."""
    return httpx.AsyncClient(timeout=10.0)


class SteamProvider:
    slug = "steam"

    def is_configured(self) -> bool:
        return bool(STEAM_API_KEY)

    def login_redirect(self, state: str) -> RedirectResponse:
        return_to = f"https://{MCP_DOMAIN}/oauth/callback/steam?state={urllib.parse.quote(state)}"
        params = urllib.parse.urlencode(
            {
                "openid.ns": "http://specs.openid.net/auth/2.0",
                "openid.mode": "checkid_setup",
                "openid.return_to": return_to,
                "openid.realm": f"https://{MCP_DOMAIN}",
                "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
                "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
            }
        )
        return RedirectResponse(f"{STEAM_OPENID_ENDPOINT}?{params}", status_code=302)

    async def resolve_callback(self, request: Request) -> ProviderResult | None:
        params = dict(request.query_params)
        if params.get("openid.mode") != "id_res":
            logger.warning("Steam callback: unexpected openid.mode=%s", params.get("openid.mode"))
            return None
        claimed_id = params.get("openid.claimed_id", "")
        if not claimed_id.startswith(STEAM_CLAIMED_ID_PREFIX):
            logger.warning("Steam callback: unexpected claimed_id shape: %s", claimed_id)
            return None
        steamid = claimed_id.removeprefix(STEAM_CLAIMED_ID_PREFIX)
        if not steamid.isdigit():
            logger.warning("Steam callback: claimed_id did not contain a numeric SteamID")
            return None

        # The security-critical step: Steam OpenID 2.0 has no client secret,
        # so a callback's authenticity is verified by POSTing the exact same
        # params back to Steam with openid.mode=check_authentication and
        # checking the response body contains "is_valid:true". Skipping
        # this lets an attacker forge a callback claiming any SteamID.
        verify_params = dict(params)
        verify_params["openid.mode"] = "check_authentication"
        try:
            async with _steam_http_client() as h:
                verify_resp = await h.post(STEAM_OPENID_ENDPOINT, data=verify_params)
                verify_resp.raise_for_status()
        except Exception as exc:
            logger.warning("Steam check_authentication error: %s", exc)
            return None
        if "is_valid:true" not in verify_resp.text:
            logger.warning("Steam callback: check_authentication rejected the response")
            return None

        display_name = steamid
        if STEAM_API_KEY:
            try:
                async with _steam_http_client() as h:
                    summary_resp = await h.get(
                        "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/",
                        params={"key": STEAM_API_KEY, "steamids": steamid},
                    )
                    summary_resp.raise_for_status()
                    players = summary_resp.json().get("response", {}).get("players", [])
                    if players:
                        display_name = players[0].get("personaname", steamid)
            except Exception as exc:
                logger.warning("Steam GetPlayerSummaries error: %s", exc)
                # Non-fatal: login still succeeds, just with the raw SteamID64.

        return ProviderResult(username=f"steam:{steamid}", display_name=display_name)


steam_provider = SteamProvider()

PROVIDERS: dict[str, IdentityProvider] = {
    "github": github_provider,
    "steam": steam_provider,
}


def configured_providers() -> list[IdentityProvider]:
    return [p for p in PROVIDERS.values() if p.is_configured()]


def get_provider(slug: str) -> IdentityProvider | None:
    return PROVIDERS.get(slug)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_identity_providers.py -v`
Expected: PASS (all tests, old and new)

- [ ] **Step 5: Commit**

```bash
git add packages/aggregator/src/aggregator/identity_providers.py packages/aggregator/tests/test_identity_providers.py
git commit -m "feat(aggregator): add SteamProvider and the provider registry"
```

---

## Task 6: `oauth.py` — provider-agnostic session/token issuing

**Files:**
- Modify: `packages/aggregator/src/aggregator/oauth.py`
- Test: `packages/aggregator/tests/test_oauth.py` (new)

**Interfaces:**
- Consumes: `access_control.is_allowed(username: str) -> bool` (Task 3).
- Produces: `oauth.finish_session(oauth_state: str, username: str) -> tuple[str, str, str] | None` (was `finish_session(github_code: str, github_state: str)` — signature changes: no longer does any HTTP exchange itself, takes an already-resolved username).

- [ ] **Step 1: Write the failing tests**

Create `packages/aggregator/tests/test_oauth.py`:

```python
"""
Tests for oauth.py's provider-agnostic session/token issuing. The actual
identity resolution (GitHub/Steam HTTP exchange) lives in
identity_providers.py and is tested there -- these tests cover
start_session/finish_session/exchange_code/rotate_refresh/validate_bearer
purely in terms of an already-resolved username.
"""

from aggregator import oauth


async def test_finish_session_issues_auth_code_for_allowed_user(monkeypatch):
    monkeypatch.setattr("aggregator.oauth.access_control.is_allowed", lambda u: True)
    state = oauth.start_session("client-1", "https://client.example/cb", "challenge", "client-state")
    result = await oauth.finish_session(state, "github:octocat")
    assert result is not None
    code, redirect_uri, client_state = result
    assert redirect_uri == "https://client.example/cb"
    assert client_state == "client-state"


async def test_finish_session_rejects_disallowed_user(monkeypatch):
    monkeypatch.setattr("aggregator.oauth.access_control.is_allowed", lambda u: False)
    state = oauth.start_session("client-1", "https://client.example/cb", "challenge", "client-state")
    result = await oauth.finish_session(state, "github:not-allowed")
    assert result is None


async def test_finish_session_rejects_unknown_state():
    result = await oauth.finish_session("not-a-real-state", "github:octocat")
    assert result is None


def _pkce_pair(verifier: str) -> tuple[str, str]:
    import base64
    import hashlib

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


async def test_exchange_code_and_validate_bearer_round_trip(monkeypatch):
    monkeypatch.setattr("aggregator.oauth.access_control.is_allowed", lambda u: True)
    verifier, challenge = _pkce_pair("a-real-code-verifier-at-least-43-characters-long")
    state = oauth.start_session("client-1", "https://client.example/cb", challenge, "cs")
    finish_result = await oauth.finish_session(state, "github:octocat")
    code, _, _ = finish_result

    result = await oauth.exchange_code(code, verifier, "client-1", "https://client.example/cb")
    assert result is not None
    access_token, refresh_token = result
    assert await oauth.validate_bearer(access_token) == "github:octocat"


async def test_exchange_code_rejects_pkce_mismatch(monkeypatch):
    monkeypatch.setattr("aggregator.oauth.access_control.is_allowed", lambda u: True)
    _, challenge = _pkce_pair("the-real-verifier-that-was-registered-at-start-session")
    state = oauth.start_session("client-1", "https://client.example/cb", challenge, "cs")
    finish_result = await oauth.finish_session(state, "github:octocat")
    code, _, _ = finish_result

    result = await oauth.exchange_code(code, "wrong-verifier", "client-1", "https://client.example/cb")
    assert result is None


async def test_validate_bearer_returns_none_for_unknown_token():
    assert await oauth.validate_bearer("not-a-real-token") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_oauth.py -v`
Expected: FAIL — `finish_session()` still requires `(github_code, github_state)` and tries to exchange with real GitHub; `TypeError` or a real network attempt.

- [ ] **Step 3: Rewrite `oauth.py`**

In `packages/aggregator/src/aggregator/oauth.py`:

Change the module docstring:

```python
"""
OAuth 2.1 + PKCE authorization server for MCP clients, backed by any
configured identity provider (identity_providers.py -- currently GitHub
and Steam).

Flow for Claude Web UI connectors:
  1. /mcp returns 401 + WWW-Authenticate pointing at /.well-known/oauth-protected-resource
  2. Claude fetches discovery documents (/.well-known/oauth-authorization-server)
  3. Claude sends user to /authorize (PKCE + CIMD client_id)
  4. We redirect the user to the configured identity provider (or a
     provider-choice page if more than one is configured)
  5. The provider redirects to /oauth/callback (GitHub) or
     /oauth/callback/steam
  6. We resolve the identity via the provider, validate it's allowed, issue
     an internal auth code
  7. We redirect to client redirect_uri with the auth code
  8. Claude POSTs to /token with code + code_verifier
  9. We verify PKCE, issue access_token + refresh_token
"""
```

Update imports — remove the now-unused `GITHUB_ALLOWED_USERS`/`GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET`, add `access_control`:

```python
import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass

import aiosqlite

from . import access_control
from .config import DB_PATH
```

(This also drops the `import httpx` line — no longer needed here, the HTTP exchange moved to `identity_providers.py`.)

Change `_AuthCode`:

```python
@dataclass
class _AuthCode:
    client_id: str
    redirect_uri: str
    code_challenge: str
    username: str
    expires_at: float
```

Replace `finish_session` entirely:

```python
async def finish_session(oauth_state: str, username: str) -> tuple[str, str, str] | None:
    """
    Given an already-resolved identity (from an IdentityProvider's
    resolve_callback), validate it's allowed and issue an internal auth
    code. Returns (auth_code, client_redirect_uri, client_state) or None on
    any failure.
    """
    session = _sessions.pop(oauth_state, None)
    if not session or session.expires_at < time.time():
        logger.warning("OAuth: unknown or expired state %s", oauth_state[:8])
        return None

    if not access_control.is_allowed(username):
        logger.warning("OAuth rejected: %s not allowed", username)
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

Update `exchange_code` (only the `_store_token` calls' 3rd argument changes name, not behavior):

```python
async def exchange_code(
    code: str, code_verifier: str, client_id: str, redirect_uri: str
) -> tuple[str, str] | None:
    """Verify auth code + PKCE, issue (access_token, refresh_token)."""
    code_obj = _codes.pop(code, None)
    if not code_obj or code_obj.expires_at < time.time():
        return None
    if code_obj.client_id != client_id or code_obj.redirect_uri != redirect_uri:
        return None
    if not verify_pkce(code_verifier, code_obj.code_challenge):
        return None

    access = secrets.token_urlsafe(48)
    refresh = secrets.token_urlsafe(48)
    await _store_token(access, "access", code_obj.username, client_id, ACCESS_TOKEN_TTL)
    await _store_token(refresh, "refresh", code_obj.username, client_id, REFRESH_TOKEN_TTL)
    logger.info("OAuth: tokens issued for %s", code_obj.username)
    return access, refresh
```

Update `rotate_refresh`:

```python
async def rotate_refresh(refresh_token: str) -> tuple[str, str] | None:
    """Validate refresh token, rotate to new (access_token, refresh_token)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT username, client_id FROM oauth_tokens "
            "WHERE token=? AND token_type='refresh' AND expires_at>?",
            (refresh_token, time.time()),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        username, client_id = row
        await db.execute("DELETE FROM oauth_tokens WHERE token=?", (refresh_token,))
        await db.commit()

    access = secrets.token_urlsafe(48)
    refresh = secrets.token_urlsafe(48)
    await _store_token(access, "access", username, client_id, ACCESS_TOKEN_TTL)
    await _store_token(refresh, "refresh", username, client_id, REFRESH_TOKEN_TTL)
    logger.info("OAuth: tokens rotated for %s", username)
    return access, refresh
```

Update `validate_bearer`:

```python
async def validate_bearer(token: str) -> str | None:
    """Return the resolved username if token is a valid OAuth access token, else None."""
    async with (
        aiosqlite.connect(DB_PATH) as db,
        db.execute(
            "SELECT username FROM oauth_tokens "
            "WHERE token=? AND token_type='access' AND expires_at>?",
            (token, time.time()),
        ) as cur,
    ):
        row = await cur.fetchone()
    return row[0] if row else None
```

Update `_store_token`:

```python
async def _store_token(
    token: str, token_type: str, username: str, client_id: str, ttl: int
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO oauth_tokens "
            "(token, token_type, username, client_id, expires_at, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (token, token_type, username, client_id, time.time() + ttl, time.time()),
        )
        await db.commit()
```

`start_session`, `verify_pkce`, `register_client`, `validate_client`, `cleanup_expired`, `protected_resource_metadata`, `authorization_server_metadata`, `www_authenticate_header` are unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_oauth.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Run the full suite to confirm no regressions so far**

Run: `cd packages/aggregator && uv run pytest`
Expected: some failures ARE expected at this point (api/oauth_router.py and admin_auth.py still call the old `finish_session`/`handle_callback` signatures — fixed in Tasks 8-9, not this one). Confirm Tasks 1-6's own tests all pass; failures should be confined to router/admin_auth-level tests.

- [ ] **Step 6: Commit**

```bash
git add packages/aggregator/src/aggregator/oauth.py packages/aggregator/tests/test_oauth.py
git commit -m "feat(aggregator): make oauth.py's session/token issuing provider-agnostic"
```

---

## Task 7: `admin_auth.py` — provider-parametrized login/callback, display name in session cookie

**Files:**
- Modify: `packages/aggregator/src/aggregator/admin_auth.py`
- Modify: `packages/aggregator/tests/test_admin_auth.py`
- Modify: `packages/aggregator/tests/test_me_endpoints.py`

**Interfaces:**
- Consumes: `identity_providers.IdentityProvider`, `identity_providers.ProviderResult` (Tasks 4-5); `access_control.is_allowed` (Task 3).
- Produces: `admin_auth.login_redirect(provider: IdentityProvider) -> RedirectResponse` (was zero-arg, GitHub-only); `admin_auth.handle_callback(request: Request, provider: IdentityProvider) -> RedirectResponse` (was one-arg, GitHub-only); `admin_auth.get_session_user(request) -> str | None` (signature unchanged, now decodes a dict-shaped cookie payload); `admin_auth.get_session_display_name(request: Request) -> str | None` (new).

- [ ] **Step 1: Write the failing tests**

Replace the full contents of `packages/aggregator/tests/test_admin_auth.py`:

```python
"""
Tests that admin_auth.get_session_user()/get_session_display_name()
correctly decode (or reject) a session cookie, that require_api_auth()
enforces personal-token auth as expected, and that login_redirect()/
handle_callback() correctly delegate to an IdentityProvider.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from aggregator import access_control, admin_auth
from aggregator.admin_auth import get_session_display_name, get_session_user, require_api_auth
from aggregator.identity_providers import ProviderResult


def _request_with_cookie(cookie_value: str) -> Request:
    scope = {
        "type": "http",
        "headers": [(b"cookie", f"admin_session={cookie_value}".encode())],
    }
    return Request(scope)


def _session_cookie(username: str, display_name: str) -> str:
    return admin_auth._signer.dumps({"username": username, "display_name": display_name})


def test_get_session_user_returns_none_for_garbage_cookie():
    assert get_session_user(_request_with_cookie("not-a-real-signed-value")) is None


def test_get_session_user_returns_none_when_no_cookie():
    assert get_session_user(Request({"type": "http", "headers": []})) is None


def test_get_session_user_returns_none_for_legacy_plain_string_payload():
    """Sessions issued before the Steam-login change signed a plain
    username string, not a {"username", "display_name"} dict. These must
    be treated as invalid (forcing re-login), not crash."""
    legacy_cookie = admin_auth._signer.dumps("octocat")
    assert get_session_user(_request_with_cookie(legacy_cookie)) is None


def test_get_session_user_returns_username_for_valid_cookie(monkeypatch):
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", set())
    cookie = _session_cookie("github:octocat", "octocat")
    assert get_session_user(_request_with_cookie(cookie)) == "github:octocat"


def test_get_session_user_returns_none_when_not_allowed(monkeypatch):
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", {"someone-else"})
    cookie = _session_cookie("github:octocat", "octocat")
    assert get_session_user(_request_with_cookie(cookie)) is None


def test_get_session_display_name_returns_display_name(monkeypatch):
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", set())
    cookie = _session_cookie("steam:76561198012345678", "CoolGamer99")
    assert get_session_display_name(_request_with_cookie(cookie)) == "CoolGamer99"


def test_get_session_display_name_returns_none_for_garbage_cookie():
    assert get_session_display_name(_request_with_cookie("garbage")) is None


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


class _FakeProvider:
    slug = "fake"

    def is_configured(self) -> bool:
        return True

    def login_redirect(self, state: str) -> RedirectResponse:
        return RedirectResponse(
            f"https://fake-provider.example/authorize?state={state}", status_code=302
        )

    async def resolve_callback(self, request: Request) -> ProviderResult | None:
        raise NotImplementedError  # overridden per-test via monkeypatch/mock


def test_login_redirect_sets_state_cookie_and_delegates_to_provider():
    response = admin_auth.login_redirect(_FakeProvider())
    assert response.status_code == 302
    assert "fake-provider.example" in response.headers["location"]
    assert "admin_oauth_state=" in response.headers.get("set-cookie", "")


async def test_handle_callback_sets_session_cookie_on_success(monkeypatch):
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", set())
    provider = _FakeProvider()
    provider.resolve_callback = AsyncMock(
        return_value=ProviderResult(username="github:octocat", display_name="octocat")
    )

    # Simulate the state round-trip: login_redirect signs+cookies a state,
    # the "callback" request carries that same state back as a query param.
    login_response = admin_auth.login_redirect(provider)
    state_cookie = login_response.headers["set-cookie"]
    # extract admin_oauth_state=<value> up to the next ';'
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


async def test_handle_callback_rejects_state_mismatch():
    provider = _FakeProvider()
    request = Request(
        {
            "type": "http",
            "query_string": b"state=wrong-state",
            "headers": [(b"cookie", b"admin_oauth_state=" + admin_auth._state_signer.dumps("real-state").encode())],
        }
    )
    response = await admin_auth.handle_callback(request, provider)
    assert response.status_code == 302
    assert "error=" in response.headers["location"]


async def test_handle_callback_rejects_when_provider_returns_none():
    provider = _FakeProvider()
    provider.resolve_callback = AsyncMock(return_value=None)
    state_token = admin_auth._state_signer.dumps("real-state")
    request = Request(
        {
            "type": "http",
            "query_string": b"state=real-state",
            "headers": [(b"cookie", f"admin_oauth_state={state_token}".encode())],
        }
    )
    response = await admin_auth.handle_callback(request, provider)
    assert response.status_code == 302
    assert "error=" in response.headers["location"]
```

Update `packages/aggregator/tests/test_me_endpoints.py`'s `_session_cookie` helper and the assertions that depend on it:

```python
def _session_cookie(username: str, display_name: str | None = None) -> str:
    return admin_auth._signer.dumps({"username": username, "display_name": display_name or username})
```

And update these two call sites (the rest of the file is unchanged):

```python
async def test_me_returns_username_and_admin_flag(client):
    client.cookies.set("admin_session", _session_cookie(USER))
    resp = await client.get("/api/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == USER
    assert body["is_admin"] is False
    assert body["display_name"] == USER
```

(every other `_session_cookie(USER)` / `_session_cookie(ADMIN)` call site in that file keeps working unchanged, since the helper's new `display_name` parameter has a default)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_admin_auth.py tests/test_me_endpoints.py -v`
Expected: FAIL — `login_redirect()`/`handle_callback()` don't accept a `provider` argument yet; `get_session_display_name` doesn't exist; `display_name` missing from `/api/me` response.

- [ ] **Step 3: Rewrite `admin_auth.py`**

Replace the full contents of `packages/aggregator/src/aggregator/admin_auth.py`:

```python
"""
Admin browser session auth: any configured IdentityProvider + itsdangerous
signed cookies.

Separate from the MCP OAuth 2.1 flow (oauth.py) which serves AI clients.
Both flows share the same IdentityProvider instances (identity_providers.py)
but use different callback-disambiguation paths.
"""

import logging
import secrets
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import access_control
from .config import SESSION_SECRET
from .identity_providers import IdentityProvider

logger = logging.getLogger(__name__)

_signer = URLSafeTimedSerializer(SESSION_SECRET, salt="admin-session")
_state_signer = URLSafeTimedSerializer(SESSION_SECRET, salt="admin-oauth-state")

SESSION_MAX_AGE = 7 * 86_400  # 7 days
STATE_MAX_AGE = 600  # 10 minutes


def _load_session_payload(request: Request) -> dict | None:
    cookie = request.cookies.get("admin_session")
    if not cookie:
        return None
    try:
        payload = _signer.loads(cookie, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    # Sessions issued before multi-provider support signed a plain string,
    # not a {"username", "display_name"} dict -- treat as invalid rather
    # than crash, forcing a one-time re-login.
    if not isinstance(payload, dict):
        return None
    return payload


def get_session_user(request: Request) -> str | None:
    """Return the authenticated (prefixed) username from the session
    cookie, or None."""
    payload = _load_session_payload(request)
    if payload is None:
        return None
    username = payload.get("username")
    if not username or not access_control.is_allowed(username):
        return None
    return username


def get_session_display_name(request: Request) -> str | None:
    """Return the cosmetic display name from the session cookie, or None.
    Never used for access control -- only get_session_user()'s return
    value is."""
    payload = _load_session_payload(request)
    if payload is None:
        return None
    return payload.get("display_name")


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


def login_redirect(provider: IdentityProvider) -> RedirectResponse:
    """Start the given identity provider's login flow for the admin
    browser session."""
    state = secrets.token_urlsafe(32)
    state_token = _state_signer.dumps(state)
    response = provider.login_redirect(state)
    response.set_cookie(
        "admin_oauth_state",
        state_token,
        httponly=True,
        max_age=STATE_MAX_AGE,
        samesite="lax",
    )
    return response


def _login_error(msg: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/login?error={urllib.parse.quote(msg)}", status_code=302)


async def handle_callback(request: Request, provider: IdentityProvider) -> RedirectResponse:
    """Handle an identity provider's callback for the admin browser flow,
    set session cookie, redirect to /admin."""
    state_token = request.cookies.get("admin_oauth_state")
    if not state_token:
        return _login_error("Missing state cookie — possible CSRF")
    try:
        stored_state = _state_signer.loads(state_token, max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return _login_error("Invalid or expired state — please try again")
    request_state = request.query_params.get("state")
    if not request_state or not secrets.compare_digest(request_state, stored_state):
        return _login_error("State mismatch — please try again")

    result = await provider.resolve_callback(request)
    if result is None:
        return _login_error("Authentication error — please try again")

    if not access_control.is_allowed(result.username):
        logger.warning("Admin login denied: %s not allowed", result.username)
        return _login_error(f"User '{result.username}' is not authorized")

    logger.info("Admin login: %s", result.username)
    session_value = _signer.dumps({"username": result.username, "display_name": result.display_name})
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

Run: `cd packages/aggregator && uv run pytest tests/test_admin_auth.py tests/test_me_endpoints.py -v`
Expected: `test_me_endpoints.py`'s `display_name` assertion still fails (main.py's `/api/me` doesn't return it yet — that's Task 9, not this one); all `test_admin_auth.py` tests pass. Confirm the failure is confined to that one new assertion.

- [ ] **Step 5: Commit**

```bash
git add packages/aggregator/src/aggregator/admin_auth.py packages/aggregator/tests/test_admin_auth.py packages/aggregator/tests/test_me_endpoints.py
git commit -m "feat(aggregator): parametrize admin_auth's login/callback by IdentityProvider, carry display name in session cookie"
```

---

## Task 8: `api/oauth_router.py` — provider chooser + per-provider callbacks

**Files:**
- Modify: `packages/aggregator/src/aggregator/api/oauth_router.py`
- Test: `packages/aggregator/tests/test_oauth_router.py` (new)

**Interfaces:**
- Consumes: `identity_providers.configured_providers()`, `identity_providers.get_provider(slug)` (Task 5); `admin_auth.handle_callback(request, provider)` (Task 7); `oauth.finish_session(state, username)` (Task 6).
- Produces: new route `GET /authorize/continue`; `GET /oauth/callback/steam`; `_authorize_handler` now branches on configured-provider count; `/oauth/callback` (GitHub) routes through a shared `_handle_oauth_callback(request, provider)`.

- [ ] **Step 1: Write the failing tests**

Create `packages/aggregator/tests/test_oauth_router.py`:

```python
"""
Tests for api/oauth_router.py's provider-aware /authorize branching and
per-provider /oauth/callback* routes. Uses a minimal FastAPI app (just
oauth_router) over httpx's ASGI transport.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aggregator import identity_providers, oauth
from aggregator.api.oauth_router import router as oauth_router
from aggregator.identity_providers import ProviderResult


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(oauth_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as c:
        yield c


def _authorize_params(**overrides) -> dict:
    params = {
        "response_type": "code",
        "client_id": "https://claude.ai",
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "state": "client-state",
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
    }
    params.update(overrides)
    return params


async def test_authorize_redirects_directly_when_one_provider_configured(client, monkeypatch):
    # oauth.validate_client() falls back to a live CIMD fetch to the
    # client's own domain before consulting the hardcoded _KNOWN_CLIENTS
    # dict -- mock it directly so this test never makes a real network call.
    monkeypatch.setattr(oauth, "validate_client", AsyncMock(return_value=True))
    monkeypatch.setattr(identity_providers, "PROVIDERS", {"github": identity_providers.github_provider})
    monkeypatch.setattr(identity_providers.github_provider, "is_configured", lambda: True)

    resp = await client.get("/authorize", params=_authorize_params())
    assert resp.status_code == 302
    assert "github.com/login/oauth/authorize" in resp.headers["location"]


async def test_authorize_shows_chooser_when_multiple_providers_configured(client, monkeypatch):
    monkeypatch.setattr(oauth, "validate_client", AsyncMock(return_value=True))
    monkeypatch.setattr(
        identity_providers,
        "PROVIDERS",
        {"github": identity_providers.github_provider, "steam": identity_providers.steam_provider},
    )
    monkeypatch.setattr(identity_providers.github_provider, "is_configured", lambda: True)
    monkeypatch.setattr(identity_providers.steam_provider, "is_configured", lambda: True)

    resp = await client.get("/authorize", params=_authorize_params())
    assert resp.status_code == 200
    assert "github" in resp.text.lower()
    assert "steam" in resp.text.lower()
    assert "/authorize/continue?provider=github" in resp.text
    assert "/authorize/continue?provider=steam" in resp.text


async def test_authorize_returns_500_when_no_provider_configured(client, monkeypatch):
    monkeypatch.setattr(oauth, "validate_client", AsyncMock(return_value=True))
    monkeypatch.setattr(identity_providers, "PROVIDERS", {})

    resp = await client.get("/authorize", params=_authorize_params())
    assert resp.status_code == 500


async def test_authorize_continue_redirects_to_named_provider(client, monkeypatch):
    monkeypatch.setattr(identity_providers.steam_provider, "is_configured", lambda: True)
    resp = await client.get("/authorize/continue", params={"provider": "steam", "state": "abc"})
    assert resp.status_code == 302
    assert "steamcommunity.com/openid/login" in resp.headers["location"]


async def test_authorize_continue_rejects_unknown_provider(client):
    resp = await client.get("/authorize/continue", params={"provider": "discord", "state": "abc"})
    assert resp.status_code == 400


async def test_oauth_callback_github_admin_flow_delegates_to_admin_auth(client, monkeypatch):
    called = {}

    async def fake_handle_callback(request, provider):
        called["provider"] = provider.slug
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/admin", status_code=302)

    monkeypatch.setattr("aggregator.api.oauth_router.admin_auth.handle_callback", fake_handle_callback)

    resp = await client.get(
        "/oauth/callback", params={"code": "abc", "state": "xyz"}, cookies={"admin_oauth_state": "present"}
    )
    assert resp.status_code == 302
    assert called["provider"] == "github"


async def test_oauth_callback_github_mcp_flow_issues_redirect_with_code(client, monkeypatch):
    monkeypatch.setattr(
        identity_providers.github_provider, "resolve_callback",
        AsyncMock(return_value=ProviderResult(username="github:octocat", display_name="octocat")),
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
    monkeypatch.setattr(oauth, "finish_session", AsyncMock(return_value=("auth-code", "https://client.example/cb", "client-state")))

    resp = await client.get("/oauth/callback/steam", params={"state": "xyz"})
    assert resp.status_code == 302
    assert "code=auth-code" in resp.headers["location"]


async def test_oauth_callback_returns_400_when_state_missing(client):
    resp = await client.get("/oauth/callback", params={"error": "access_denied"})
    assert resp.status_code == 400


async def test_oauth_callback_returns_403_when_provider_resolve_fails(client, monkeypatch):
    monkeypatch.setattr(
        identity_providers.github_provider, "resolve_callback", AsyncMock(return_value=None)
    )
    resp = await client.get("/oauth/callback", params={"error": "access_denied", "state": "xyz"})
    assert resp.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_oauth_router.py -v`
Expected: FAIL — `/authorize/continue` and `/oauth/callback/steam` don't exist (404); `_authorize_handler` still hardcodes GitHub.

- [ ] **Step 3: Rewrite `api/oauth_router.py`**

Replace the full contents of `packages/aggregator/src/aggregator/api/oauth_router.py`:

```python
"""OAuth 2.1 endpoints — included at root (no prefix)."""

import html
import urllib.parse

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import admin_auth, identity_providers, oauth

router = APIRouter()


# ── Discovery ─────────────────────────────────────────────────────────────────


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    return oauth.protected_resource_metadata()


@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server():
    return oauth.authorization_server_metadata()


# ── Dynamic Client Registration (RFC7591) ─────────────────────────────────────


@router.post("/register")
async def oauth_register(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    redirect_uris = body.get("redirect_uris")
    if not redirect_uris or not isinstance(redirect_uris, list):
        return JSONResponse(
            {"error": "invalid_redirect_uri", "error_description": "redirect_uris required"},
            status_code=400,
        )

    client_id = body.get("client_id", "")
    registration = oauth.register_client(client_id, redirect_uris)
    return JSONResponse(registration, status_code=201)


# ── Authorization request ─────────────────────────────────────────────────────


def _provider_choice_page(state: str) -> HTMLResponse:
    links = "".join(
        f'<p><a href="/authorize/continue?provider={html.escape(p.slug)}'
        f'&state={urllib.parse.quote(state)}">Continue with {html.escape(p.slug.capitalize())}</a></p>'
        for p in identity_providers.configured_providers()
    )
    return HTMLResponse(f"<h1>Choose a login method</h1>{links}")


async def _authorize_handler(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    scope: str,
):
    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
    if code_challenge_method != "S256":
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Only S256 supported"},
            status_code=400,
        )
    if not await oauth.validate_client(client_id, redirect_uri):
        return JSONResponse({"error": "invalid_client"}, status_code=400)

    session_state = oauth.start_session(client_id, redirect_uri, code_challenge, state)
    configured = identity_providers.configured_providers()
    if not configured:
        return JSONResponse(
            {"error": "server_error", "error_description": "No identity provider configured"},
            status_code=500,
        )
    if len(configured) == 1:
        return configured[0].login_redirect(session_state)
    return _provider_choice_page(session_state)


@router.get("/authorize")
async def oauth_authorize(
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    state: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query(...),
    scope: str = Query(default="mcp"),
):
    return await _authorize_handler(
        response_type,
        client_id,
        redirect_uri,
        state,
        code_challenge,
        code_challenge_method,
        scope,
    )


# Alias kept for clients that cached old discovery documents
@router.get("/oauth/authorize")
async def oauth_authorize_alias(
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    state: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query(...),
    scope: str = Query(default="mcp"),
):
    return await _authorize_handler(
        response_type,
        client_id,
        redirect_uri,
        state,
        code_challenge,
        code_challenge_method,
        scope,
    )


@router.get("/authorize/continue")
async def authorize_continue(provider: str = Query(...), state: str = Query(...)):
    """Reached from the provider-choice page (only rendered when more than
    one provider is configured) -- forwards the pending PKCE session's
    state to whichever provider the user picked."""
    p = identity_providers.get_provider(provider)
    if p is None or not p.is_configured():
        return JSONResponse(
            {"error": "invalid_request", "error_description": "unknown or unconfigured provider"},
            status_code=400,
        )
    return p.login_redirect(state)


# ── Provider callbacks ──────────────────────────────────────────────────────────
# Both the admin browser flow and the MCP OAuth flow redirect here. The
# admin flow sets an `admin_oauth_state` cookie before leaving for the
# provider, which serves as the discriminator. GitHub's callback path is
# unchanged (`/oauth/callback`) so existing OAuth App registrations don't
# need updating; Steam gets its own new path since its callback params are
# shaped completely differently (openid.* instead of code/state).


async def _handle_oauth_callback(request: Request, provider: identity_providers.IdentityProvider):
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

    finish_result = await oauth.finish_session(state, result.username)
    if not finish_result:
        return HTMLResponse(
            "<h1>Access denied</h1><p>Authentication failed or user is not authorized.</p>",
            status_code=403,
        )

    auth_code, redirect_uri, client_state = finish_result
    qs = urllib.parse.urlencode({"code": auth_code, "state": client_state})
    return RedirectResponse(f"{redirect_uri}?{qs}", status_code=302)


@router.get("/oauth/callback")
async def oauth_callback(request: Request):
    return await _handle_oauth_callback(request, identity_providers.github_provider)


@router.get("/oauth/callback/steam")
async def oauth_callback_steam(request: Request):
    return await _handle_oauth_callback(request, identity_providers.steam_provider)


# ── Token endpoint ────────────────────────────────────────────────────────────


async def _token_handler(
    grant_type: str,
    code: str | None,
    code_verifier: str | None,
    client_id: str | None,
    redirect_uri: str | None,
    refresh_token: str | None,
):
    if grant_type == "authorization_code":
        if not all([code, code_verifier, client_id, redirect_uri]):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        result = await oauth.exchange_code(code, code_verifier, client_id, redirect_uri)
        if not result:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access, new_refresh = result
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": oauth.ACCESS_TOKEN_TTL,
            "refresh_token": new_refresh,
            "scope": "mcp",
        }

    if grant_type == "refresh_token":
        if not refresh_token:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        result = await oauth.rotate_refresh(refresh_token)
        if not result:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access, new_refresh = result
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": oauth.ACCESS_TOKEN_TTL,
            "refresh_token": new_refresh,
            "scope": "mcp",
        }

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


@router.post("/token")
async def oauth_token(
    grant_type: str = Form(...),
    code: str = Form(None),
    code_verifier: str = Form(None),
    client_id: str = Form(None),
    redirect_uri: str = Form(None),
    refresh_token: str = Form(None),
):
    return await _token_handler(
        grant_type, code, code_verifier, client_id, redirect_uri, refresh_token
    )


# Alias kept for clients that cached old discovery documents
@router.post("/oauth/token")
async def oauth_token_alias(
    grant_type: str = Form(...),
    code: str = Form(None),
    code_verifier: str = Form(None),
    client_id: str = Form(None),
    redirect_uri: str = Form(None),
    refresh_token: str = Form(None),
):
    return await _token_handler(
        grant_type, code, code_verifier, client_id, redirect_uri, refresh_token
    )
```

Note what changed structurally from the original: `GITHUB_CLIENT_ID`/`MCP_DOMAIN` are no longer imported here at all (the GitHub-specific redirect-building moved into `GitHubProvider.login_redirect`); `_authorize_handler` no longer builds any provider's redirect URL directly — it only decides *whether* to redirect immediately or show the chooser, and delegates the actual redirect-building to whichever `IdentityProvider` is involved.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_oauth_router.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add packages/aggregator/src/aggregator/api/oauth_router.py packages/aggregator/tests/test_oauth_router.py
git commit -m "feat(aggregator): provider-aware /authorize chooser and per-provider oauth callbacks"
```

---

## Task 9: `main.py` — Steam admin-login route, `/api/auth/providers`, `display_name` in `/api/me`, startup guard

**Files:**
- Modify: `packages/aggregator/src/aggregator/main.py`
- Modify: `packages/aggregator/tests/conftest.py`
- Modify: `packages/aggregator/tests/test_me_endpoints.py`

**Interfaces:**
- Consumes: `identity_providers.configured_providers()`, `identity_providers.PROVIDERS`, `identity_providers.get_provider("steam")` (Task 5); `admin_auth.get_session_display_name` (Task 7).
- Produces: `GET /admin/login/steam`; `GET /api/auth/providers`; `/api/me` response gains `"display_name"`.

- [ ] **Step 1: Write the failing test**

In `packages/aggregator/tests/test_me_endpoints.py`, append:

```python
async def test_auth_providers_reflects_configured_state(client, monkeypatch):
    from aggregator import identity_providers

    monkeypatch.setattr(identity_providers.github_provider, "is_configured", lambda: True)
    monkeypatch.setattr(identity_providers.steam_provider, "is_configured", lambda: False)
    resp = await client.get("/api/auth/providers")
    assert resp.status_code == 200
    assert resp.json() == {"github": True, "steam": False}
```

(the `display_name` assertion added to `test_me_returns_username_and_admin_flag` back in Task 7 is also verified by this task's implementation — it should now pass)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_me_endpoints.py -v`
Expected: FAIL — `test_auth_providers_reflects_configured_state` gets 404 (`GET /api/auth/providers` doesn't exist); `test_me_returns_username_and_admin_flag`'s `display_name` assertion fails (missing key).

- [ ] **Step 3: Update `conftest.py` with dummy GitHub credentials**

This task adds a hard startup check ("at least one provider configured, or the app refuses to start") to `main.py`'s `lifespan()`. `test_mcp_access_integration.py` boots the real app via a genuine uvicorn server (unlike `test_me_endpoints.py`'s bare `ASGITransport`, which never runs `lifespan()` at all) — it needs at least one provider to appear configured, without making any real network call. Add to `packages/aggregator/tests/conftest.py`, in the same env-var-setup block at the top (before any `aggregator.*` import):

```python
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="aggregator-test-data-"))
os.environ.setdefault("ADMIN_USERS", "test-admin")
os.environ.setdefault("GITHUB_CLIENT_ID", "test-github-client-id")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-github-client-secret")
```

(these values are never used to make a real request to github.com in the test suite — they only need to be non-empty so `GitHubProvider.is_configured()` returns `True`, satisfying the new startup guard)

- [ ] **Step 4: Update `main.py`**

Change the import block:

```python
from . import access_control, admin_auth, identity_providers, log_capture, oauth
```

Change `lifespan()` — add the guard as the very first thing, before `init_db()`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    if not identity_providers.configured_providers():
        raise RuntimeError(
            "No identity provider configured -- set GITHUB_CLIENT_ID+GITHUB_CLIENT_SECRET "
            "and/or STEAM_API_KEY."
        )
    await init_db()
    servers = await list_servers()
    for srv in servers:
        if srv.enabled:
            try:
                await child_manager.add(srv)
            except Exception as exc:
                logger.error("Could not start %s on boot: %s", srv.name, exc)
    cleanup_task = asyncio.create_task(_token_cleanup_loop())
    async with streamable_manager.run():
        yield
    cleanup_task.cancel()
    for name in list(child_manager._children):
        await child_manager.remove(name)
```

Add the new `/admin/login/steam` route next to the existing `/admin/login/github` one:

```python
@app.get("/admin/login/github")
async def admin_login_github():
    return admin_auth.login_redirect(identity_providers.github_provider)


@app.get("/admin/login/steam")
async def admin_login_steam():
    return admin_auth.login_redirect(identity_providers.steam_provider)
```

Add the new `/api/auth/providers` route (near `/api/me`):

```python
@app.get("/api/auth/providers")
async def api_auth_providers():
    return {slug: provider.is_configured() for slug, provider in identity_providers.PROVIDERS.items()}
```

Change `/api/me` to include `display_name`:

```python
@app.get("/api/me")
async def api_me(request: Request):
    user = admin_auth.get_session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "username": user,
        "is_admin": access_control.is_admin(user),
        "display_name": admin_auth.get_session_display_name(request),
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_me_endpoints.py -v`
Expected: PASS (all tests, old and new)

- [ ] **Step 6: Run the full suite**

Run: `cd packages/aggregator && uv run pytest -v`
Expected: PASS — every test in the aggregator package. This is the point where the whole feature should be fully wired end-to-end on the backend.

- [ ] **Step 7: Commit**

```bash
git add packages/aggregator/src/aggregator/main.py packages/aggregator/tests/conftest.py packages/aggregator/tests/test_me_endpoints.py
git commit -m "feat(aggregator): wire Steam admin login, /api/auth/providers, display_name, startup provider guard"
```

---

## Task 10: Webui — types, API client, `useAuthProviders` hook

**Files:**
- Modify: `packages/webui/src/lib/types.ts`
- Modify: `packages/webui/src/lib/api.ts`
- Create: `packages/webui/src/hooks/useAuthProviders.ts`

**Interfaces:**
- Produces: `Me.display_name: string | null`; `AuthProviders` type (`Record<string, boolean>`); `api.authProviders() -> Promise<AuthProviders>`; `useAuthProviders()` hook.

- [ ] **Step 1: Edit `types.ts`**

Change `Me`:

```typescript
export interface Me {
  username: string;
  is_admin: boolean;
  display_name: string | null;
}
```

Add, after `Me`:

```typescript
export type AuthProviders = Record<string, boolean>;
```

- [ ] **Step 2: Edit `api.ts`**

Add `AuthProviders` to the type import:

```typescript
import type {
  AddServerInput,
  AddServerResult,
  AuthProviders,
  CallToolInput,
  CallToolResult,
  GenerateTokenResult,
  Me,
  ServerConfig,
  ToolInfo,
} from "./types";
```

Add to the `api` object (near `me`):

```typescript
  authProviders: () => request<AuthProviders>("/api/auth/providers"),
```

- [ ] **Step 3: Create `useAuthProviders.ts`**

Create `packages/webui/src/hooks/useAuthProviders.ts`:

```typescript
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useAuthProviders() {
  return useQuery({ queryKey: ["auth-providers"], queryFn: api.authProviders });
}
```

- [ ] **Step 4: Verify the build**

Run: `cd packages/webui && pnpm build`
Expected: succeeds (nothing consumes the new fields/hook yet, so this only confirms the additions themselves are syntactically/type valid)

- [ ] **Step 5: Commit**

```bash
git add packages/webui/src/lib/types.ts packages/webui/src/lib/api.ts packages/webui/src/hooks/useAuthProviders.ts
git commit -m "feat(webui): add display_name/AuthProviders types, authProviders API call and hook"
```

---

## Task 11: Webui — `LoginPage` shows a button per configured provider

**Files:**
- Modify: `packages/webui/src/components/LoginPage.tsx`

**Interfaces:**
- Consumes: `useAuthProviders()` (Task 10).

- [ ] **Step 1: Rewrite `LoginPage.tsx`**

Replace the full contents of `packages/webui/src/components/LoginPage.tsx`:

```tsx
import { loginRoute } from "@/router";
import { useAuthProviders } from "@/hooks/useAuthProviders";

const PROVIDER_LABELS: Record<string, string> = {
  github: "GitHub",
  steam: "Steam",
};

export function LoginPage() {
  const { error } = loginRoute.useSearch();
  const { data: providers } = useAuthProviders();
  const enabled = Object.entries(providers ?? {}).filter(([, on]) => on);

  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="w-full max-w-sm space-y-4 text-center">
        <h1 className="text-2xl font-semibold">MCP Aggregator</h1>
        <p className="text-muted-foreground">Sign in to access the admin interface.</p>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <div className="space-y-2">
          {enabled.map(([slug]) => (
            <a
              key={slug}
              href={`/admin/login/${slug}`}
              className="block rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
            >
              Login with {PROVIDER_LABELS[slug] ?? slug}
            </a>
          ))}
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify the build**

Run: `cd packages/webui && pnpm build`
Expected: succeeds

- [ ] **Step 3: Manual verification**

With the backend and `just webui-dev` both running (per the README's dev instructions), and with only `GITHUB_CLIENT_ID`/`SECRET` set: confirm the login page shows one "Login with GitHub" button and no Steam button. This can't be fully exercised without real GitHub/Steam credentials configured; at minimum confirm the page renders without errors and shows/hides buttons correctly by toggling which env vars are set locally.

- [ ] **Step 4: Commit**

```bash
git add packages/webui/src/components/LoginPage.tsx
git commit -m "feat(webui): show a login button per configured identity provider"
```

---

## Task 12: Webui — show display name instead of raw username

**Files:**
- Modify: `packages/webui/src/components/AppLayout.tsx`
- Modify: `packages/webui/src/components/AccountPage.tsx`

**Interfaces:**
- Consumes: `Me.display_name` (Task 10).

- [ ] **Step 1: Edit `AppLayout.tsx`**

Change:

```tsx
            <span>{me?.username}</span>
```

to:

```tsx
            <span>{me?.display_name ?? me?.username}</span>
```

- [ ] **Step 2: Edit `AccountPage.tsx`**

Change:

```tsx
          <span>{me?.username}</span>
```

to:

```tsx
          <span>{me?.display_name ?? me?.username}</span>
```

- [ ] **Step 3: Verify the build**

Run: `cd packages/webui && pnpm build`
Expected: succeeds

- [ ] **Step 4: Commit**

```bash
git add packages/webui/src/components/AppLayout.tsx packages/webui/src/components/AccountPage.tsx
git commit -m "feat(webui): show the session's display name instead of the raw (possibly prefixed) username"
```

---

## Task 13: Docs & config — README, `.env.example`, `docker-compose.yml`, `scripts/init-env.sh`, `CLAUDE.md`

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `docker-compose.yml`
- Modify: `scripts/init-env.sh`
- Modify: `CLAUDE.md`

No test — documentation/config only.

- [ ] **Step 1: Edit `.env.example`**

Add after the existing `GITHUB_ALLOWED_USERS`/`ADMIN_USERS` block:

```
# ── Steam login (optional alternative to GitHub) ────────────────────────────
# Get a free key at: https://steamcommunity.com/dev/apikey
# Its presence enables Steam login; without it, only GitHub is offered.
# At least one of GitHub or Steam must be configured.
STEAM_API_KEY=

# Comma-separated list of raw SteamID64s allowed to authenticate (not
# prefixed -- unlike ADMIN_USERS, see below). Optional -- leave empty to
# allow any Steam account to log in once STEAM_API_KEY is set.
STEAM_ALLOWED_USERS=
```

Update the `ADMIN_USERS` comment to reflect the now-prefixed format:

```
# Comma-separated list of admins with full rights (see and manage every
# server, override visibility). Values must be prefixed by identity
# provider: "github:octocat" or "steam:76561198012345678" -- NOT bare
# usernames. Optional — leave empty for no admins.
# If upgrading from a version without Steam login: existing unprefixed
# ADMIN_USERS values (e.g. "octocat") must be changed to "github:octocat",
# or that admin silently loses admin rights.
ADMIN_USERS=
```

Also update the `GITHUB_CLIENT_ID`/`SECRET` comment block to note they're now optional:

```
# GitHub OAuth App credentials (optional if Steam login is configured
# instead -- at least one of GitHub or Steam is required)
# Register at: https://github.com/settings/developers → OAuth Apps
# Homepage URL:                https://<MCP_DOMAIN>
# Authorization callback URL:  https://<MCP_DOMAIN>/oauth/callback
#   (shared by admin browser login and MCP client OAuth flow)
GITHUB_CLIENT_ID=
GITHUB_CLIENT_SECRET=
```

- [ ] **Step 2: Edit `docker-compose.yml`**

Add to the `environment` block:

```yaml
      STEAM_API_KEY: "${STEAM_API_KEY}"
      STEAM_ALLOWED_USERS: "${STEAM_ALLOWED_USERS}"
```

- [ ] **Step 3: Edit `scripts/init-env.sh`**

In the interactive block, add prompts after the existing `ADMIN_USERS` one:

```bash
    read -rp "Admin GitHub username(s) [comma-separated, optional]: " ADMIN_USERS
    read -rp "Steam Web API key [optional, leave empty to skip Steam login]: " STEAM_API_KEY
    read -rp "Steam allowed SteamID64(s) [comma-separated, optional]: " STEAM_ALLOWED_USERS
```

In the non-interactive `else` block, add:

```bash
    ADMIN_USERS=""
    STEAM_API_KEY=""
    STEAM_ALLOWED_USERS=""
```

In the heredoc, add after the `ADMIN_USERS=${ADMIN_USERS}` line:

```

# ── Steam login (optional alternative to GitHub) ────────────────────────────
# Get a free key at: https://steamcommunity.com/dev/apikey
STEAM_API_KEY=${STEAM_API_KEY}
STEAM_ALLOWED_USERS=${STEAM_ALLOWED_USERS}
```

Also update the note that `ADMIN_USERS` values must now be prefixed — add a comment line right above `ADMIN_USERS=${ADMIN_USERS}` in the heredoc:

```
# Values must be prefixed: "github:octocat" or "steam:76561198012345678".
```

- [ ] **Step 4: Edit `README.md`**

1. Line 3 (project description): change `with GitHub OAuth authentication` to `with GitHub or Steam authentication`.
2. Line 11 (architecture diagram): no change needed — already provider-agnostic (`OAuth 2.1 + PKCE OR Bearer <personal token>`).
3. Lines 27-30 (Auth model bullets): replace with:
   ```
   **Auth model:**
   - **Browser → `/admin`, `/api`** — GitHub or Steam login via signed session cookies (whichever is configured; both may be enabled at once). Only allowed identities (`GITHUB_ALLOWED_USERS` / `STEAM_ALLOWED_USERS`) get in.
   - **MCP client → `/mcp`** — OAuth 2.1 + PKCE (Claude Web UI connectors, choosing a provider if more than one is configured) or a personal API token (Claude Desktop etc. — generate one from the webui's Account page after logging in).
   - **Claude Web UI** — Full OAuth 2.1 flow: discovery → dynamic client registration → PKCE authorize → provider login → token exchange. No manual token needed.
   - **Identity across providers** — a GitHub login and a Steam login are always separate identities in this system (`"github:octocat"` vs `"steam:76561198012345678"`) — there's no account linking. `ADMIN_USERS` values must include the provider prefix.
   ```
4. Line 36 (Prerequisites): change `- A GitHub OAuth App (see setup below)` to `- A GitHub OAuth App and/or a Steam Web API key (see setup below) — at least one is required`.
5. After the existing "### 2. Register a GitHub OAuth App" section (ends around line 63), add a new subsection:
   ```markdown
   ### 2b. (Optional) Get a Steam Web API key

   Steam login is an alternative to GitHub — configure either one, or both.

   Go to **steamcommunity.com/dev/apikey**, sign in, and request a key (any
   domain name works for the "Domain Name" field, it's not validated
   strictly). Copy the key into `.env` as `STEAM_API_KEY`.

   Unlike GitHub's OAuth App, Steam's OpenID 2.0 login needs no callback URL
   registration — it works immediately once `STEAM_API_KEY` is set.
   ```
6. Line 80 (local GitHub OAuth App callback note): unchanged — Steam needs no equivalent registration step, already covered by the new 2b section.
7. Environment Variables table (around lines 428-433): update the `GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET`/`GITHUB_ALLOWED_USERS` rows' "Required" column from ✅ to `— *`, add a footnote, and add three new rows:
   ```markdown
   | `GITHUB_CLIENT_ID` | — * | GitHub OAuth App Client ID |
   | `GITHUB_CLIENT_SECRET` | — * | GitHub OAuth App Client Secret |
   | `GITHUB_ALLOWED_USERS` | — | Comma-separated list of allowed GitHub usernames (unprefixed) |
   | `STEAM_API_KEY` | — * | Steam Web API key — its presence enables Steam login |
   | `STEAM_ALLOWED_USERS` | — | Comma-separated list of allowed raw SteamID64s (unprefixed) |
   | `ADMIN_USERS` | — | Comma-separated **prefixed** identities with admin rights (e.g. `github:octocat,steam:76561198012345678`) |

   \* At least one of (`GITHUB_CLIENT_ID` + `GITHUB_CLIENT_SECRET`) or `STEAM_API_KEY` is required — the app refuses to start with neither configured.
   ```
8. Access model callout (around lines 222-230, the one describing per-server ownership/visibility): update the `ADMIN_USERS` reference to note the prefixed format, e.g. change `Only the owner or an admin (\`ADMIN_USERS\`)` context to add a footnote-style clarification if the surrounding text names example usernames — check the actual current text at that location and adjust any bare-username examples to prefixed ones (e.g. `octocat` → `github:octocat`) for consistency.

- [ ] **Step 5: Edit `CLAUDE.md`**

Add a line near the other aggregator-specific gotchas:

```
- Two identity providers exist (GitHub, Steam) behind a shared `IdentityProvider` abstraction (`identity_providers.py`). Identity strings are always prefixed (`"github:x"` / `"steam:y"`) and never linked across providers — the same human logging in with both gets two unrelated identities. `ADMIN_USERS` must use the prefixed form; `GITHUB_ALLOWED_USERS`/`STEAM_ALLOWED_USERS` stay unprefixed (raw login / raw SteamID64).
```

- [ ] **Step 6: Verify no stray old-format references remain**

Run: `grep -rn "GITHUB_CLIENT_ID.*✅\|Register at.*github.com.*Only" README.md` (sanity spot-check, expect no output matching the old required-checkmark table row) and manually re-read the edited sections for coherence.

- [ ] **Step 7: Commit**

```bash
git add README.md .env.example docker-compose.yml scripts/init-env.sh CLAUDE.md
git commit -m "docs: document Steam login setup, prefixed ADMIN_USERS, optional GitHub credentials"
```

---

## Final verification

- [ ] Run the full backend suite: `cd packages/aggregator && uv run pytest -v`. Expected: all pass.
- [ ] Run lint: `cd packages/aggregator && uvx ruff check src tests`. Expected: clean.
- [ ] Run format check: `cd packages/aggregator && uvx ruff format --check src tests`. Expected: clean (fix with `uvx ruff format src tests` if not).
- [ ] Run the webui build: `cd packages/webui && pnpm build`. Expected: succeeds.
- [ ] Run the webui lint: `cd packages/webui && pnpm lint`. Expected: only the three pre-existing `only-export-components` warnings, nothing new.
- [ ] Grep for any leftover `github_user` reference outside historical spec/plan docs: `grep -rn "github_user" --include="*.py" packages/aggregator/src packages/aggregator/tests`. Expected: no output.
