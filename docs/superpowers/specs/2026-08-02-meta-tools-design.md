# Design: native "meta" MCP tools for self-management

**Date:** 2026-08-02
**Status:** Approved, not yet implemented

## Context

tiny-metamcp aggregates MCP servers and re-exposes their tools on `/mcp`,
namespaced `<server>__<tool>`. Managing which servers are configured (add,
remove, enable/disable, restart, check status) is currently only possible
through the browser admin panel (`/admin`, session-cookie auth) or the REST
API (`/api/*`, session cookie or `ADMIN_TOKEN` bearer) — never through
`/mcp` itself.

The goal: let an MCP client (Claude Desktop, Claude Web UI, etc.) manage
the aggregator's own server registry conversationally, by calling MCP
tools on the same `/mcp` endpoint it already uses to call proxied tools.

## Access model (important, non-obvious)

Today, an MCP OAuth access token (issued via the GitHub-login PKCE flow to
e.g. Claude Web UI) only ever reaches `/mcp` — it is never accepted by
`/api/*` (`require_api_auth` only accepts an admin session cookie or
`ADMIN_TOKEN`; see `admin_auth.py`). Exposing management as MCP tools is a
deliberate expansion: any client that can already call a proxied tool
(i.e. holds a valid MCP OAuth token for a `GITHUB_ALLOWED_USERS` member, or
the static `ADMIN_TOKEN`) will also be able to add/remove/enable/disable/
restart servers.

This is intentional — it's the whole point of the feature (conversational
admin via an LLM client) — not an oversight. It's safe under the existing
trust model because `GITHUB_ALLOWED_USERS` already gates who can obtain an
MCP OAuth token at all; this doesn't add a new class of user, it grants an
existing, already-vetted class of user (people who can talk to the
aggregator's MCP endpoint at all) a capability they didn't have before.
No new auth branch is introduced — meta tools use exactly the same
`_check_bearer` gate on `/mcp` that proxied tool calls already go through.

## Sequencing

This is pure backend work — no webui/UI component. It targets the
**current** file structure (`aggregator/src/mcp_aggregator/`), not the
in-progress monorepo/webui restructure's target layout. When that restructure
lands, these files move with everything else via its planned `git mv`; no
special handling needed here.

## Design

### Tool naming — no prefix

Proxied tools are always namespaced `<server>__<tool>` (they always contain
a `__` separator, enforced by `aggregator.py`'s `handle_list_tools`). Meta
tools use **plain, unprefixed names** — `list_servers`, `add_server`,
`delete_server`, `enable_server`, `disable_server`, `restart_server` — which
can never collide with a proxied tool's name by construction. This was
chosen over a `meta__`-prefixed scheme specifically because it needs no
reserved-word validation (e.g. blocking a child server from being named
`meta`) — one less thing to validate, one less edge case.

### New file: `aggregator/src/mcp_aggregator/meta_tools.py`

Exports:
- `TOOLS: list[mcp.types.Tool]` — static tool definitions (name,
  description, inputSchema), one entry per tool below.
- `NAMES: frozenset[str]` — `{t.name for t in TOOLS}`, used by
  `aggregator.py` to decide whether an incoming `call_tool` name is a meta
  tool before falling through to child resolution.
- `async def call(name: str, arguments: dict) -> list[types.TextContent]`
  — dispatches to the six handlers below by name, returns a single
  `TextContent` block containing a JSON-serialized result (so an LLM can
  parse a predictable, REST-API-shaped payload rather than free text).

Internally, `meta_tools.py` imports and calls straight into the existing
`database.py` functions (`list_servers`, `add_server`, `get_server`,
`update_server_enabled`, `delete_server`) and `child_manager.child_manager`
— the exact same functions `api/routers.py` already uses. It adds one new
local helper, `_find_by_name(name: str) -> Server`, since the existing DB
functions operate by numeric id and every meta tool below takes a `name`
(more natural for conversational use than an opaque id) — this iterates
`await list_servers()` and matches on `.name`, raising `ValueError(f"No
server named {name!r}")` if not found. No new `database.py` function is
needed for this — a name is not a supported lookup key there today, and
it's not worth adding one just to save an in-memory scan over what will
typically be a handful of rows.

### Tools

**`list_servers`** — no arguments. Returns a JSON array, one object per
configured server: `{id, name, type, package, args, env, enabled, running,
tool_count, error}` — the same shape `GET /api/servers` returns today
(built the same way `api/routers.py::_cfg()` + `child_manager.status()`
combine it, reimplemented locally rather than importing that private
helper across modules).

```json
{"type": "object", "properties": {}}
```

**`add_server`** — `name` (string, required), `type` (string, required,
one of the live `ServerType` enum values — `pypi`/`npm`/`git`/`cmd`, plus
`proxy` once that type lands per the separate proxy-server-type plan; no
hardcoded enum list here, `ServerType(type_str)` naturally accepts
whatever is defined), `package` (string, required), `args` (array of
strings, optional, default `[]`), `env` (object of string→string,
optional, default `{}`). Saves to DB via `database.add_server()`, then
attempts `child_manager.add()` exactly like `POST /api/servers` does: the
DB row is kept even if the child fails to start, and the failure is
surfaced in the response rather than raised. Returns
`{"server": {...cfg}, "tools": [...], "error": str | null}`. A duplicate
name raises the underlying DB error, caught and re-raised as
`ValueError(str(exc))` for a readable message instead of a raw SQLAlchemy
traceback.

```json
{
  "type": "object",
  "properties": {
    "name": {"type": "string"},
    "type": {"type": "string"},
    "package": {"type": "string"},
    "args": {"type": "array", "items": {"type": "string"}, "default": []},
    "env": {"type": "object", "additionalProperties": {"type": "string"}, "default": {}}
  },
  "required": ["name", "type", "package"]
}
```

**`delete_server`** — `name` (string, required). Resolves via
`_find_by_name`, stops the running child (`child_manager.remove`), runs
`installer.uninstall()` (git checkout cleanup), deletes the DB row.
Returns `{"deleted": name}`.

**`enable_server`** — `name` (string, required). Resolves via
`_find_by_name`, calls `database.update_server_enabled(id, True)`, then
attempts `child_manager.add()`. Returns
`{"name": name, "enabled": true, "tool_count": int}` on success; if the
child fails to start, the enable still succeeds (matches
`admin_enable`/`api_enable_server`'s existing behavior of logging a
warning rather than failing the request) and the response includes
`"error": str`.

**`disable_server`** — `name` (string, required). Resolves via
`_find_by_name`, stops the child, calls
`database.update_server_enabled(id, False)`. Returns
`{"name": name, "enabled": false}`.

**`restart_server`** — `name` (string, required). Resolves via
`_find_by_name`, calls `child_manager.restart(name)`. Raises
`ValueError(f"Server {name!r} is not running")` if `child_manager.restart`
raises `KeyError` (not currently started — matches the existing
`api_restart_server` 404 behavior, translated to a tool-call error instead
of an HTTP status). Returns `{"name": name, "tool_count": int}`.

`delete_server`/`enable_server`/`disable_server`/`restart_server` share
this input schema:

```json
{
  "type": "object",
  "properties": {"name": {"type": "string"}},
  "required": ["name"]
}
```

### Changes to `aggregator.py`

`handle_list_tools()` gains `tools.extend(meta_tools.TOOLS)` alongside the
existing per-child loop (order: meta tools first, then proxied tools — an
arbitrary but stable choice, not load-bearing).

`handle_call_tool()` gains an early branch:

```python
if name in meta_tools.NAMES:
    return await meta_tools.call(name, arguments or {})
```

placed before the existing `child_manager.resolve(name)` call. Everything
else in that function is unchanged.

## Error handling

Every meta tool raises a plain `ValueError` with a human-readable message
on failure (unknown server name, invalid type, duplicate name, restart on
a non-running server) — the same style `aggregator.py` already uses for
"No running server found for tool: ...". The MCP SDK's `@call_tool()`
decorator converts an uncaught exception into a protocol-level tool error
automatically; no manual try/except-and-wrap is needed in `meta_tools.py`
beyond the one explicit DB-duplicate-name re-raise noted above (needed
only because SQLAlchemy's raw exception text is not a clean single-line
message).

## Testing

No automated test suite exists in this project (same situation as the
proxy-server-type plan). Verify manually with a real MCP client — e.g.
`npx @modelcontextprotocol/inspector` pointed at `http://localhost:8000/mcp`
with `Authorization: Bearer <ADMIN_TOKEN>` (per the README's existing local
testing instructions) — call `list_servers`, `add_server` (with a real
`pypi` package), confirm the new server appears in the admin UI's servers
table, `disable_server` / `enable_server` it, `restart_server` it, then
`delete_server` it and confirm it's gone from both the tool call result and
the admin UI.
