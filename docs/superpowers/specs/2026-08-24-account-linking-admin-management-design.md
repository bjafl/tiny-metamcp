# Design: Account linking across providers + admin-editable user lists

**Date:** 2026-08-24
**Status:** Approved, not yet implemented

## Context

Steam login (already shipped, `docs/superpowers/specs/2026-08-23-steam-login-design.md`)
deliberately kept every provider identity separate: `"github:octocat"` and
`"steam:76561198012345678"` are two unrelated accounts in this system, and
`GITHUB_ALLOWED_USERS`/`STEAM_ALLOWED_USERS`/`ADMIN_USERS` are env vars —
editable only by redeploying.

This design changes both of those decisions:

1. **Account linking** — a person can log in with either GitHub or Steam
   and land on the same account, proven by logging in to both while
   already authenticated (self-service, no admin involvement).
2. **Admin-editable user management** — allow-lists and admin rights move
   from env vars to a database, editable from the webui. Env vars become
   **one-time seed values** for a fresh install, not an ongoing source of
   truth.

Builds on the `IdentityProvider` abstraction from the Steam work without
changing it — GitHub/Steam login itself (redirect, `check_authentication`,
token exchange) is untouched. This design only changes what happens
*after* a provider confirms an identity.

## Data model

Three new tables in `packages/aggregator/src/aggregator/models.py`:

```python
from sqlalchemy import UniqueConstraint


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
    provider: str        # "github" / "steam"
    raw_id: str           # unprefixed provider-native id (GitHub login / SteamID64)
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
```

`AllowedIdentity` is the DB-backed replacement for
`GITHUB_ALLOWED_USERS`/`STEAM_ALLOWED_USERS`/`ADMIN_USERS`: a row means
"this raw provider identity may create an account on first login" (and
optionally "...as an admin"). **Empty for a given provider still means
unrestricted** for that provider — identical to today's env-var semantics,
preserved deliberately for upgrade continuity (see "Config and seeding"
below). An admin can add a row here *before* the person has ever logged
in — this is the pre-approval mechanism.

At most one `UserIdentity` per `(provider, raw_id)` — an identity belongs
to exactly one user. At most one identity per provider per user is
enforced at the application layer (see "Account linking" below), not the
schema, so a future third provider needs no schema change.

### Canonical identity

`Server.owner_username`, `PersonalToken.username`, and the session cookie
keep their existing `str` columns/shape (no type change — minimizes blast
radius across the codebase), but the value they hold changes from
`"github:octocat"` to `"user:<id>"` — provider-independent, stable across
which provider was used to log in on any given day.

### Migration (runs in `database.init_db()`, in this order)

1. **Format-convert existing identity data.** For every distinct
   `"provider:raw"` string currently in `servers.owner_username` or
   `personal_tokens.username`: create a `User` + matching `UserIdentity`
   (if one doesn't already exist for that `provider`/`raw_id` pair), then
   rewrite the column value to `"user:<id>"`. This is a pure format
   conversion — no accounts are merged by this step. Merging only ever
   happens through the self-service linking flow, going forward.
2. **Seed env vars, exactly once.** Gated by a dedicated marker (see
   below), not "table is empty" — an admin who deliberately empties
   `AllowedIdentity` later must not have it silently repopulated by a
   stale `.env` on the next restart. For each `GITHUB_ALLOWED_USERS`/
   `STEAM_ALLOWED_USERS` entry: insert an `AllowedIdentity` row. For each
   `ADMIN_USERS` entry (`"provider:raw"`): if step 1 already created a
   `User` for that identity, set `is_admin=True` on it; otherwise create a
   new `User(is_admin=True)` + `UserIdentity`.

The one-time-seed marker: a small `AuthSeedState` table with a single row
(`id=1, seeded: bool`), checked and set inside the same transaction as
step 2. Table-emptiness is not used as the gate, because step 1 can
already have populated `users`/`user_identities` on an upgrading
deployment even when step 2 (env-var seeding) has never run.

## Login resolution

`access_control.py` gains an async, DB-backed resolution function,
replacing the call sites that used to call the old, purely in-memory
`is_allowed(prefixed_string)` directly after a provider callback:

```python
async def resolve_login(provider: str, raw_id: str, display_name: str) -> str | None:
    """Given a freshly-verified provider identity, return the canonical
    "user:<id>" session identity, or None if this person may not log in.
    Auto-provisions a new User on first login for identities covered by
    the allow-list (or when that provider's allow-list is empty --
    unrestricted, matching today's semantics)."""
```

Resolution order:
1. `UserIdentity(provider, raw_id)` already exists → load its `User`,
   check `.allowed`.
2. Not found → check `AllowedIdentity` (empty set for that provider =
   unrestricted) → if allowed, create `User` (+ `is_admin` from
   `grant_admin` if a matching `AllowedIdentity` row exists) and the
   `UserIdentity`. If a matching `AllowedIdentity` row was consulted, it
   is deleted once consumed — keeps the admin's "pending identities" list
   meaning only genuinely-not-yet-used pre-approvals, not a growing log of
   everyone who's ever signed up.
3. Neither → `None` (403, same external behavior as today's denial path).

**This is the one deliberate, load-bearing architectural shift in this
design**: `is_admin()`, `can_manage()`, `_is_visible()`,
`validate_personal_token()` all move from pure synchronous checks against
in-memory env-var sets to async DB lookups (parse the `id` out of
`"user:<id>"`, load the `User` row). This preserves the existing principle
that **every request re-checks current state** — an admin revoking access
takes effect immediately, no session invalidation or cache to wait out —
just against SQLite instead of a Python set. Given this is a self-hosted,
low-QPS MCP proxy, the extra per-request SQLite read is not treated as a
performance concern; no caching layer is introduced.

**Documented behavior change: per-request "is the provider still
configured" revocation no longer applies at the session/token level.**
Today, `is_allowed()` re-checks `provider_impl.is_configured()` on every
request, so un-configuring a provider (e.g. removing `STEAM_API_KEY`)
immediately kills every standing session/token tied to that provider. A
canonical `"user:<id>"` session carries no record of which provider it was
established through, so that specific mechanism can no longer be expressed
once accounts can be reached via more than one identity. The replacement:
`resolve_login` still refuses a **new** login attempt through a
currently-unconfigured provider (checked per attempt, same as today), and
un-configuring a provider still means no one can **link** it going
forward — but an already-authenticated session/token for an account that
also has another, still-configured identity linked keeps working, since
the account itself was never the thing that got disabled. An operator who
wants to fully cut a specific person off should use the account-level
`allowed` toggle (see "Admin webui" below) — that check still runs on
every request, exactly as before, just keyed to the account instead of to
a single provider identity.

## Account linking (self-service)

New Account-page UI: a "Link GitHub" / "Link Steam" button per configured
provider the current user hasn't already linked.

**Flow:**
1. `GET /admin/link/{provider}` — requires an active, valid session (401
   otherwise). Sets a new signed cookie `link_identity_state` (same
   `itsdangerous`-signed pattern as today's `admin_oauth_state`) carrying
   `{state, user_id}`, where `user_id` comes from the *already
   authenticated* session — unforgeable without the server's signing key.
   Redirects to the provider's normal `login_redirect(state)`.
2. Provider confirms the identity exactly as it does for a normal login
   (`check_authentication` for Steam, token exchange for GitHub) — no
   changes to `IdentityProvider.resolve_callback`.
3. The shared `_handle_oauth_callback` dispatcher (`api/oauth_router.py`)
   gains a third branch, checked before the existing two: a present
   `link_identity_state` cookie means this is a **link callback**, not an
   admin login or an MCP PKCE flow.
4. Link resolution: if `(provider, raw_id)` already belongs to a
   *different* user → reject with a clear error (no forced merge via
   self-service — that tradeoff was made explicitly: linking is
   self-service only, no admin override). If it already belongs to the
   *same* user → no-op success (idempotent re-link). Otherwise → create
   the `UserIdentity` row against the `user_id` from the cookie.
5. At most one identity per provider per user — attempting to link a
   second GitHub account while one is already linked is rejected; the
   existing one must be unlinked first.

**Unlinking** (small addition beyond the literal request, needed so a
mis-link isn't permanent): a "Remove" action per linked identity on the
Account page. Refuses to remove a user's last remaining identity — that
would make the account permanently inaccessible.

## Admin webui: user management

New **"Users"** tab, admin-only (same gating pattern as other admin-only
UI elements today):

- **User table** — one row per `User`: linked identities as badges (e.g.
  `github:octocat`, `steam:76561…`), an "Admin" toggle, an "Allowed"
  toggle, created date. Changes apply immediately via
  `PATCH /api/users/{id}` — no separate save step, matching the existing
  `ServerTable` visibility-select pattern.
- **Pending/allowed identities** — a second section listing
  `AllowedIdentity` rows: add a raw GitHub login or SteamID64 (with an
  optional "grant admin on creation" checkbox) *before* that person has
  ever logged in — the DB-backed replacement for hand-editing
  `GITHUB_ALLOWED_USERS`/`STEAM_ALLOWED_USERS`/`ADMIN_USERS`.
  `POST`/`DELETE /api/allowed-identities`.
- **Self-lockout guard**: an admin cannot remove their own admin rights or
  set their own `allowed=false` through this UI.

**Explicitly out of scope for this design**: hard-deleting a `User` row
(would orphan their owned servers/personal tokens). The `allowed` toggle
covers revocation; real deletion can be a later, separate addition if it
turns out to be needed.

## Config and seeding

`GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET`/`STEAM_API_KEY` are provider
*credentials* — untouched by this design, still plain env vars.

`GITHUB_ALLOWED_USERS`/`STEAM_ALLOWED_USERS`/`ADMIN_USERS` change role:
from always-authoritative to **one-time seed values only** (see
Migration, step 2, and the `AuthSeedState` marker). After that first run,
editing these in `.env`/Coolify has **no effect whatsoever** — the
database is the sole source of truth from then on.

The README gets an explicit, prominent note about this (same pattern as
the "Upgrading" section added for Steam login) — this is exactly the
class of silent surprise ("why doesn't changing `.env` do anything")
flagged as a real risk during design, so it must be stated plainly, not
buried in an `.env.example` comment.

## Testing

Following this project's established convention (real local flows over
mocking, except where there's genuinely nothing local to test against —
none of that exception applies here, this is all local DB + in-process
logic):

- **Migration**: legacy-shaped SQLite file (same `tmp_path`/
  `sqlite3.connect` pattern already used by
  `_migrate_server_columns`/`_migrate_identity_prefixes`) — confirm old
  `"provider:raw"` strings become correct `User`+`UserIdentity` rows and
  `owner_username`/`personal_tokens.username` are rewritten to
  `"user:<id>"`; confirm `ADMIN_USERS`/`*_ALLOWED_USERS` seed correctly;
  confirm seeding is genuinely idempotent (run the migration twice, no
  duplicate rows, no re-seed after the marker is set).
- **`resolve_login`**: matrix — known identity (existing user); unknown +
  allow-listed (auto-provisions); unknown + not allow-listed (rejected);
  `allowed=False` on an existing user (rejected even though the identity
  itself would otherwise pass the allow-list check).
- **Self-service linking**: happy path (new identity attaches to the
  logged-in user's `user_id`); conflict (identity already linked to a
  *different* user → rejected); idempotent re-link to the same user;
  unauthenticated attempt to start the link flow → 401; attempt to remove
  the last remaining identity → rejected.
- **Admin API**: `PATCH /api/users/{id}` correctly toggles
  `is_admin`/`allowed`; the self-lockout guard (can't demote/disable your
  own account) is covered; `AllowedIdentity` CRUD.
- **Webui**: no new frontend test suite, consistent with prior features in
  this project.
