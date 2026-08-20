# Design: per-user server access control

**Date:** 2026-08-20
**Status:** Approved, not yet implemented

## Context

Today there is no per-user concept anywhere in the app. `GITHUB_ALLOWED_USERS`
is a flat allowlist — every listed GitHub username gets identical, full
access: the entire admin webui, the whole REST API, and management rights
(add/edit/delete/enable/disable/restart) over every server. The static
`ADMIN_TOKEN` bearer credential (used by non-browser MCP clients like Claude
Desktop) is even more anonymous — a shared secret with no identity behind it
at all.

This design introduces:

- An **admin** role, distinct from regular allowed users.
- Per-server **visibility**: a server is either visible to everyone, or
  private to its owner (and admins).
- The choice is made when a server is added, and an admin can change it
  later on any server.
- Personal API tokens, replacing the shared `ADMIN_TOKEN`, so every `/mcp`
  connection has a real user identity to filter by.

Enforcement has to reach three surfaces that already independently manage
servers (established in `2026-08-04-edit-server-config-design.md`): the REST
API, the MCP meta-tools, and the aggregated `/mcp` tool list/dispatch itself.

## Data model

`Server` (`models.py`) gains two columns:

```python
class ServerVisibility(StrEnum):
    EVERYONE = "everyone"
    PRIVATE = "private"

class Server(SQLModel, table=True):
    ...
    owner_username: str | None = Field(default=None)
    visibility: str = Field(default=ServerVisibility.EVERYONE.value)
```

Existing rows migrate to `owner_username=NULL`, `visibility="everyone"` —
preserves today's "everyone sees everything" behavior for servers that
predate this feature. Since `owner_username` is `NULL` for those rows and
management requires `owner_username == username` (see below), pre-existing
servers become admin-managed-only going forward; any regular user could
manage them before this feature shipped. This is a deliberate, disclosed
behavior change, not an oversight.

New table:

```python
class PersonalToken(SQLModel, table=True):
    __tablename__ = "personal_tokens"

    username: str = Field(primary_key=True)
    token_hash: str = Field(unique=True, index=True)
    created_at: float = Field(default_factory=_time.time)
```

Only a SHA-256 hash is stored; the raw token is shown once at generation
time, the same pattern as a GitHub personal access token.

### Migration mechanics

`database.py::init_db()` currently does only
`SQLModel.metadata.create_all(...)`, which creates missing *tables* but does
not alter an existing `servers` table to add new columns — there is no
migration framework in this project (no Alembic). `init_db()` gets a small
manual step after `create_all`: inspect `PRAGMA table_info(servers)` and run
`ALTER TABLE servers ADD COLUMN owner_username TEXT` /
`ALTER TABLE servers ADD COLUMN visibility TEXT DEFAULT 'everyone'` for
whichever columns are missing. `PersonalToken` is a new table, so
`create_all` handles it with no extra step.

## Identity & auth

New env var `ADMIN_USERS` (comma-separated, same format as
`GITHUB_ALLOWED_USERS`), read into `config.py` as a `set[str]`. It is
expected to be a subset of `GITHUB_ALLOWED_USERS` but this isn't enforced —
an admin username outside the allowlist simply can never log in, so it's
inert rather than dangerous. `access_control.py::is_admin(username) -> bool`
checks membership.

`ADMIN_TOKEN` is removed entirely: `config.py`, the `_check_bearer` branch in
`main.py`, `.env.example`, `docker-compose.yml`, `scripts/init-env.sh`, and
every README reference.

Both places that resolve a bearer credential to an identity change from
"validate and return None" to "validate and return the username":

- `admin_auth.require_api_auth` (REST API / webui) — becomes
  `require_api_auth(request) -> str`, raising 401 as before on failure.
- `main.py::_check_bearer` (`/mcp`, `/messages`) — tries
  `oauth.validate_bearer(token)` (existing GitHub OAuth 2.1 flow, already
  resolves to a `github_user`) then a new `access_control.validate_personal_token(token)`
  (hash lookup against `PersonalToken`), returns the resolved username or
  raises 401.

### Carrying identity into `/mcp` request handling

`aggregator.py`'s `mcp_server` is one shared, stateless `Server` instance
reused across every concurrent `/mcp` connection (per its own docstring) —
there is no per-connection object to attach a username to, and
`handle_list_tools`/`handle_call_tool`'s `ServerRequestContext` parameter
doesn't carry custom application data here.

The fix is a `contextvars.ContextVar[str]`, e.g. `_current_user`, defined in
`aggregator.py`. `main.py::_check_bearer` sets it immediately after
resolving the username, before calling `mcp_server.run(...)` /
`streamable_manager.handle_request(...)` / `sse_transport.handle_post_message(...)`.
Because each incoming request runs in its own asyncio task and `ContextVar`
values are copied into new tasks rather than shared globally, this stays
correctly isolated per concurrent connection even though `mcp_server` itself
is long-lived and shared. `handle_list_tools`/`handle_call_tool` read
`_current_user.get()`.

Personal tokens and OAuth tokens are otherwise interchangeable at this
layer — both resolve to a plain `username: str`.

### Self-service token endpoints

- `GET /api/me` (exists today) extended to return `{"username", "is_admin"}`.
- `POST /api/me/token` — generates a new personal token, replacing any
  existing one for that user (one active token per user, matching the
  earlier `ADMIN_TOKEN` mental model). Returns the raw token once; only the
  hash is persisted. No separate revoke-without-replace endpoint — YAGNI,
  since generating a new token already invalidates the old one for the only
  case that matters (an untrusted client holding the old token).

## Access control module

A new `packages/aggregator/src/aggregator/access_control.py` is the single
source of truth for visibility/ownership rules, imported by both
`meta_tools.py` and `routers.py` so the rule isn't duplicated:

```python
async def visible_servers(username: str) -> list[Server]: ...
async def visible_server_names(username: str) -> set[str]: ...
def can_manage(server: Server, username: str) -> bool: ...
def is_admin(username: str) -> bool: ...
```

`visible_servers`: admins get every row; everyone else gets rows where
`visibility == "everyone"` plus their own rows (`owner_username == username`)
regardless of visibility. `can_manage`: true iff `is_admin(username)` or
`server.owner_username == username`.

### Enforcement points

**`meta_tools.py`** — `call(name, arguments, username)` gains the `username`
parameter (passed explicitly by the caller, not read from the `ContextVar`
directly — keeps these as plain, independently testable functions with no
hidden dependency on `aggregator.py`'s context state):

- `_list_servers` filters through `visible_servers(username)`.
- `_find_by_name` (used by edit/delete/enable/disable/restart) only matches
  servers the caller can manage; a private server owned by someone else
  raises the same `ValueError(f"No server named {name!r}")` used today for a
  genuinely missing name, so its existence isn't leaked.
- `_add_server` gains a `visibility` argument, and stores `owner_username`
  as the calling user. Schema default for `visibility` is `"private"` — a
  safer default than today's implicit "everyone can see it."
- The `add_server`/`edit_server` tool input schemas add a `visibility`
  enum property (`"everyone"` / `"private"`).

**`aggregator.py`** — `handle_list_tools` reads `_current_user.get()`, passes
it to `meta_tools.call(...)` for the meta-tools branch, and filters
`child_manager.all_tools()` down to `visible_server_names(username)` before
building the returned tool list. `handle_call_tool` performs the same
visible-set check before dispatching a `<server>__<tool>` call to a child —
this closes the gap where a client that already knows a private server's
tool name (from a previous session, or a guess) could otherwise call it
directly even though it's absent from the current tool list.

**`routers.py`** (REST API, backs the webui) — every route in this router
already sits behind `Depends(require_api_auth)`; each handler additionally
takes `username: str = Depends(require_api_auth)` to scope its query.

- `/servers*`: `GET /servers` filters through `visible_servers`; the
  mutating routes (`PATCH`, `DELETE`, `/enable`, `/disable`, `/restart`)
  404 (not 403 — consistent with the not-leaking-existence stance above) if
  `can_manage` is false. `AddServerRequest` and `ServerUpdateRequest` gain a
  `visibility: ServerVisibility` field. Admins get "assign access" for free
  from this: since `can_manage` is already true for admins on every server,
  an admin can flip `visibility` on anyone's server through the existing
  `PATCH /servers/{id}` endpoint — no separate endpoint or ACL table needed.
- `/tools*` (backs the webui's Tool Tester): `GET /tools` currently lists
  every child's tools straight from `child_manager.all_tools()` with no
  scoping at all — it gets the same `visible_server_names(username)` filter
  as `/mcp`'s `handle_list_tools`. `POST /tools/call` currently looks up
  `req.server` directly in `child_manager` with no scoping — it gets the
  same visibility check as `/mcp`'s `handle_call_tool` (visibility, i.e.
  usage access, not `can_manage` — a user merely granted access to an
  "everyone"-visible server can already call its tools today via `/mcp`,
  and the REST Tool Tester should behave identically): 404 if `req.server`
  isn't in `visible_server_names(username)`.
- `/logs*`: also currently unscoped — reads straight from `log_capture`
  with no relation to `Server` visibility at all. `GET /logs` and
  `GET /logs/stream` filter to `visible_server_names(username)` when no
  `server` query param is given, and 404 (stream: reject before opening the
  SSE connection) if an explicit `server` param isn't in that set.
  `GET /logs/{server_name}/stderr` 404s the same way.

## Webui

- **Add Server dialog** (`AddServerDialog.tsx`): new visibility control
  (Everyone / Just me radio or switch), defaulting to "Just me". Present on
  both add and edit (an owner or admin can change visibility later through
  the same edit flow described in `2026-08-04-edit-server-config-design.md`).
- **Server table**: regular users see only servers `visible_servers` returns
  for them. Admins see every server, with an added "Owner" column and a
  visibility badge/toggle they can flip inline (reuses the edit mutation).
- **New "My Account" page**: shows the current username, an admin badge if
  applicable, a "Generate/Regenerate token" action (raw token shown once,
  copy-to-clipboard), and an updated Claude Desktop config snippet using the
  user's own personal token in place of the retired `ADMIN_TOKEN`.

## Docs & config

- README: remove every `ADMIN_TOKEN` reference; document `ADMIN_USERS`,
  server visibility semantics, and the personal-token self-service flow;
  update the `curl` examples in the REST API section to use a personal
  token instead of `ADMIN_TOKEN`.
- `.env.example`, `docker-compose.yml`, `scripts/init-env.sh`: drop
  `ADMIN_TOKEN` generation/wiring; document `ADMIN_USERS` as optional
  (comma-separated, subset of `GITHUB_ALLOWED_USERS`).

## Testing

Following the project's existing convention (real local MCP server over
mocked transport — see `tests/conftest.py`'s `proxy_target_url` fixture):

- `access_control.py` unit tests covering the visibility/ownership matrix:
  admin/owner/stranger × everyone/private.
- `meta_tools` tests extended with a `username` parameter, covering the same
  matrix for `list_servers`/`add_server`/`edit_server`/`delete_server`, plus
  the "not found, not forbidden" error-shape assertion for a private
  server's name.
- An integration test against `/mcp` using two distinct personal tokens
  (two `PersonalToken` rows) to confirm tool-list filtering and call
  rejection actually happen end-to-end through the `ContextVar` plumbing —
  the kind of behavior a mocked transport would likely miss, per this
  project's existing testing guidance.
- REST API tests for the new `visibility` field on `POST`/`PATCH /servers`,
  for 404-on-no-access on the mutating `/servers` routes, and for the same
  scoping on `GET /tools`, `POST /tools/call`, and `/logs*`.

No frontend test suite exists for `packages/webui` today; not adding one as
part of this feature, consistent with `2026-08-04-edit-server-config-design.md`.
