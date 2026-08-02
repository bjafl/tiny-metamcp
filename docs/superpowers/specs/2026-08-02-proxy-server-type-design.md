# Design: `proxy` server type — pure proxy to an already-running MCP server

**Date:** 2026-08-02
**Status:** Approved, not yet implemented — depends on the in-progress monorepo/webui restructure

## Context

tiny-metamcp aggregates MCP servers behind a single endpoint. Today every
`Server` has a `type` of `pypi`, `npm`, `git`, or `cmd`, and all four are
spawned as **local subprocesses**: `installer.build_command()` turns the
config into a shell command, and `ChildState.start()` (in
`child_manager.py`) runs it via `stdio_client` from the MCP Python SDK.

The goal of this feature: add a fifth type, **`proxy`**, that instead of
spawning a subprocess, connects directly to an MCP server that is **already
running elsewhere on the network** (e.g. another container on the same
Docker network) via **Streamable HTTP**, and re-exposes its tools through
the aggregator exactly like any other child server — pure passthrough, no
local process involved.

## Dependency on the monorepo/webui restructure

This feature is designed against the **post-restructure** layout described
in `docs/superpowers/plans/2026-08-02-monorepo-webui.md` (in progress as of
this writing, not yet merged):

- Backend package moves + renames: `aggregator/src/mcp_aggregator/*` →
  `packages/aggregator/src/aggregator/*`.
- The server-rendered Jinja2/htmx admin panel (`templates/admin/*`) is
  deleted entirely and replaced by a React SPA at `packages/webui`.

**Sequencing decision:** implement this feature *after* the monorepo/webui
plan has landed, targeting the new file layout below. Do not implement it
against the current `aggregator/src/mcp_aggregator` + Jinja admin layout —
that would mean redoing the UI half of the work once the other plan's
Task 4 deletes the Jinja templates.

**Known risk to re-check at implementation time:** the monorepo/webui plan
bumps the `mcp` SDK dependency to `mcp>=2` (resolves to `mcp==2.0.0`) and
flags a known, unrelated, out-of-scope bug where `aggregator.py`'s use of
`mcp.server.Server` no longer matches that version's API. This design's
client-side API calls (`mcp.client.streamable_http.streamable_http_client`)
were verified against `mcp==1.27.1` (the version currently pinned). Re-verify
the signature against whatever `mcp` version is actually pinned once this
work starts — the SDK's streamable HTTP client API may have shifted between
1.x and 2.0.0 the same way the server API did.

## Design

### Data model

No new DB columns. `Server.package` is reused to hold the target URL (e.g.
`http://other-service:8000/mcp`) — the same pattern already used today,
where `package` holds a git URL for the `git` type and a raw command for
`cmd`. `args` and `env` stay unused for this type, matching how `args` is
already unused for some `cmd` configs.

### Backend changes

1. **`packages/aggregator/src/aggregator/models.py`**
   Add `ServerType.PROXY = "proxy"` to the enum.

2. **`packages/aggregator/src/aggregator/child_manager.py`**
   `ChildState.start()` branches on `config.type == ServerType.PROXY`:
   - Skip `installer.install()` and `installer.build_command()` — there is
     no subprocess to install or launch.
   - Skip opening a per-child stderr log file (`log_capture.open_log_file`)
     — there is no child process stderr to capture. Connection
     success/failure is still logged via the existing `clog.info`/
     `clog.error` calls.
   - Use `mcp.client.streamable_http.streamable_http_client(config.package)`
     as the async context manager to obtain `(read, write)` streams,
     instead of `stdio_client(params)`.
   - Everything downstream is unchanged: `ClientSession(read, write)`,
     `session.initialize()`, `session.list_tools()`, storing the streams'
     `AsyncExitStack` on `ChildState._stack`.
   - `stop()`, `restart()`, `status()`, `resolve()`, `all_tools()` need no
     changes — they already operate generically on `ChildState.session`
     and the `AsyncExitStack`, regardless of what transport opened it.
     Closing the stack for a Streamable HTTP connection sends a session
     termination `DELETE` (SDK default `terminate_on_close=True`), which is
     the correct passthrough-teardown behavior.

3. **`packages/aggregator/src/aggregator/installer.py`**
   No change. `install()` and `build_command()` are simply not called for
   the `proxy` type (handled in `child_manager.py` above).

### Frontend changes

4. **`packages/webui/src/lib/types.ts`**
   Extend the `ServerType` union: `"pypi" | "npm" | "git" | "cmd" | "proxy"`.

5. **`packages/webui/src/components/AddServerDialog.tsx`**
   Add `proxy` as an option in the type `Select`. Update the `package`
   field's label/placeholder to mention the URL form for `proxy`
   (e.g. `http://host:port/mcp`).

No other webui changes needed — `ServerTable.tsx` and `ServersPage.tsx`
already render `type`/`package` generically from the API response.

## Error handling

No retry/backoff for the `proxy` type. A failed connection (bad URL, remote
unreachable) surfaces as `ChildState.error`, exactly like a bad `pypi`
package name does today — visible in the servers table, clearable via the
existing restart action. This is a deliberate scope cut: the target network
(local Docker network) is trusted, and this project's other server types
have no auto-reconnect either, so `proxy` shouldn't introduce a different
error-recovery model than its siblings.

## Auth

Out of scope for this iteration — no support for headers/bearer tokens to
the remote MCP server. The local Docker network is treated as the trust
boundary. Can be added later (the SDK's `streamable_http_client` accepts a
custom `httpx.AsyncClient` with headers pre-configured, which is a
non-breaking extension point) if a remote server ever needs it.

## Testing

The project has no automated test suite currently, and this feature doesn't
introduce one in isolation. Verify manually:

1. Run a simple Streamable HTTP MCP server as another service on the local
   Docker network.
2. Add it via `POST /api/servers` with `type=proxy` and `package` set to
   its URL (or via the webui Add Server dialog once Task 8 of the other
   plan has landed).
3. Confirm `GET /api/tools` lists its tools with the `<server>__<tool>`
   namespacing, and `POST /api/tools/call` round-trips a real call through
   the aggregator to the remote server and back.
4. Confirm `POST /api/servers/{id}/restart` and delete/disable work the
   same as for a subprocess-backed server.
