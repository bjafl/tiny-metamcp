# Design: edit MCP server configs

**Date:** 2026-08-04
**Status:** Approved, not yet implemented

## Context

`Server` configs (`name`, `type`, `package`, `args`, `env`, `enabled`) are
managed today through three parallel surfaces that all funnel into
`database.py` + `child_manager.py`:

- REST API (`packages/aggregator/src/aggregator/api/routers.py`) —
  `GET /servers`, `POST /servers`, `DELETE /servers/{id}`,
  `POST /servers/{id}/enable`, `POST /servers/{id}/disable`,
  `POST /servers/{id}/restart`.
- MCP meta-tools (`packages/aggregator/src/aggregator/meta_tools.py`) —
  `list_servers`, `add_server`, `delete_server`, `enable_server`,
  `disable_server`, `restart_server`.
- Admin webui (`packages/webui/src/components/AddServerDialog.tsx`,
  `ServerTable.tsx`) — add-server dialog, table row actions for
  restart/enable/disable/delete.

None of these support editing an existing config. `enabled` is the only
field with a dedicated update path (`database.py::update_server_enabled`).
To change `name`, `type`, `package`, `args`, or `env` today, a user must
delete and re-add the server, losing its `id`. This design adds a proper
edit capability across all three surfaces.

## Data model & API contract

No DB schema change — `Server` (`models.py:16-31`) already has every field
an edit needs. Add a request model for partial updates, distinct from
`AddServerRequest` (which requires every field):

```python
class ServerUpdateRequest(BaseModel):
    name: str | None = None
    type: ServerType | None = None
    package: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
```

New endpoint: `PATCH /servers/{id}` in `routers.py`, next to the existing
`POST /servers`. Semantics:

- **Partial update.** A field present in the body overwrites the
  corresponding column. A field omitted from the body keeps its current
  DB value.
- **Wholesale replace per field, no deep merge.** If `env` is present in
  the request, it replaces the entire stored `env` dict — it is not
  merged key-by-key with the existing one. Same for `args`. This keeps the
  semantics simple and avoids "was this key omitted or explicitly
  cleared?" ambiguity.
- **`name` uniqueness still enforced.** Renaming to a name already in use
  by another server fails the same way `add_server` does today (raw
  SQLAlchemy integrity error, caught and re-raised as `ValueError`,
  surfaced as a 4xx from the route).
- All fields are editable, including `name` and `type` — there is no
  locked subset.

## Database layer

Add `database.py::update_server(id, **fields) -> Server`: fetch the row,
apply only the fields actually passed (mirroring the `Server.get_args()` /
`get_env()` JSON-encode-on-write pattern already used by `add_server`),
commit, return the updated `Server`. Generalizes the existing
`update_server_enabled`, which stays as-is (still used by the
enable/disable endpoints).

## Apply-on-edit flow

No new `child_manager` method is needed — the edit flow composes existing
primitives, in `routers.py`'s `PATCH` handler (and the mirrored meta-tool
handler):

1. Persist the change via `update_server()`.
2. If the server currently has a running child: `child_manager.remove(old_name)`.
   Using the *old* name here matters — if `name` is part of the edit,
   children are keyed by name (`ChildManager._children`,
   `child_manager.py:291`), so the old key must be explicitly removed or
   it leaks.
3. If the (possibly just-updated) `enabled` is true: `child_manager.add(new_config)`
   — the same call `add_server`/`enable_server` already use, which itself
   stops-then-starts anything still present under the new name.
4. Return the same `_cfg()`-shaped response the other mutating endpoints
   return: config fields plus `running`/`tool_count`/`error` pulled from
   `child_manager.status()`.

**Failure handling:** no rollback. If the new config fails to start (bad
package, unreachable proxy URL, etc.), the DB already has the new config
and the old child is already stopped; `status()` reports `running: false`
and an `error` string, exactly like a failed `add_server` or `restart`
does today. The user re-edits to fix it. This matches existing behavior
for add/restart failures and avoids the complexity of trial-starting a
config before committing it.

## MCP meta-tool

Add `edit_server(id, name=None, type=None, package=None, args=None, env=None)`
to `meta_tools.py`, following the existing `add_server`/`delete_server`
pattern: same partial-update semantics as the REST endpoint, same
underlying `update_server()` + apply-on-edit orchestration, gated by the
same MCP OAuth bearer token as the other meta tools (per the access model
in `2026-08-02-meta-tools-design.md`).

## Admin webui

Extend `AddServerDialog` to accept an optional `server` prop instead of
adding a separate component:

- `server` absent → today's "Add Server" behavior (prefilled empty,
  submits `POST /servers`).
- `server` present → prefills all fields from it, dialog title and submit
  button read "Edit Server", submit calls a new mutation hitting
  `PATCH /servers/{id}`, and invalidates the same query key the add
  mutation already invalidates on success.

`ServerTable.tsx` gets a new "Edit" row action, opening the dialog with
`server` set to that row, alongside the existing
Restart/Enable-Disable/Delete actions.

Note: the REST API returns raw `env` values (`routers.py`'s `_cfg`), unlike
the meta-tools' redacted `"***"` display (`meta_tools.py::_cfg`) — the
webui edit form will show real env values when prefilling, which is
consistent with how the Add dialog and server table already behave today.

## Testing

Aggregator pytest cases for `PATCH /servers/{id}`, using the project's
real-local-MCP-server fixture pattern (`tests/conftest.py`'s
`proxy_target_url`) rather than mocking transport, per existing project
convention:

- Partial field update (e.g. only `env` changes; `args`/`package` persist
  unchanged).
- Rename to an already-used name → conflict error, no partial mutation.
- Edit while the server is running/enabled → old child stopped, new child
  started with the new config, response reflects new `running`/`tool_count`.
- Edit while disabled → DB updates, `child_manager` untouched (no start
  attempted).
- Edit targeting a nonexistent `id` → 404.

No frontend test suite currently exists for `packages/webui`; not adding
one as part of this feature unless requested.
