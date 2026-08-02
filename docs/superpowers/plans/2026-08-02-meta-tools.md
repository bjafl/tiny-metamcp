# Native Meta MCP Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any authenticated `/mcp` client manage the aggregator's own server registry (list/add/delete/enable/disable/restart) by calling plain MCP tools, without a `meta__` namespace prefix, reusing the existing `database.py`/`child_manager.py` functions in-process.

**Architecture:** A new `meta_tools.py` module owns six tool definitions and their handlers, calling straight into the same `database.py` and `child_manager.py` functions `api/routers.py` already uses. `aggregator.py`'s `handle_list_tools()`/`handle_call_tool()` gain a few lines each to fold these in ahead of the existing per-child dispatch.

**Tech Stack:** Python (FastAPI, `mcp` SDK, SQLModel) — `packages/aggregator/src/aggregator/`.

## Global Constraints

- **Reuse existing DB/child-manager functions as-is.** No `update_server()`/edit capability — confirmed out of scope in the spec.
- **No `meta__` prefix.** Tool names are plain (`list_servers`, `add_server`, etc.) — proxied tools always contain `__`, so there is no collision surface and no reserved-word validation is needed.
- **No new auth branch.** Meta tools are reachable by anything that can already reach `/mcp` (OAuth-authenticated `GITHUB_ALLOWED_USERS` member or `ADMIN_TOKEN`) — this is intentional, see the spec's "Access model" section.
- **No automated test suite exists in this project.** Verification below is manual/scripted, run and read by the engineer, not a committed pytest suite.
- **The `mcp==2.0.0` API break is fixed.** `packages/aggregator/src/aggregator/aggregator.py` was migrated (commit `56f9d00`) from the old decorator-based `@mcp_server.list_tools()`/`@mcp_server.call_tool()` registration to `mcp==2.0.0`'s constructor-injected `on_list_tools`/`on_call_tool` callbacks, which take a `(ctx, params)` signature and return typed `ListToolsResult`/`CallToolResult` objects instead of bare lists. `import aggregator.main` succeeds as of this revision. **This changes Task 2's integration point below** compared to this plan's original draft — see the updated diffs, which target the actual current handler signatures, not the pre-migration decorator style.

---

### Task 1: `meta_tools.py` — tool definitions and handlers

**Files:**
- Create: `packages/aggregator/src/aggregator/meta_tools.py`

**Interfaces:**
- Consumes: `database.list_servers/add_server/update_server_enabled/delete_server` (existing, `database.py`), `child_manager.child_manager.add/remove/restart/status` (existing, `child_manager.py`), `installer.uninstall` (existing, `installer.py`), `models.Server/ServerType` (existing, `models.py`).
- Produces: `TOOLS: list[mcp.types.Tool]`, `NAMES: frozenset[str]`, `async def call(name: str, arguments: dict) -> list[mcp.types.TextContent]` — consumed by Task 2's `aggregator.py` changes.

- [ ] **Step 1: Write `meta_tools.py`**

```python
"""
Native MCP tools for managing the aggregator's own server registry.

Unlike proxied tools (always namespaced `<server>__<tool>`), these use
plain names — there's no collision surface since proxied names always
contain `__` and these never do.
"""

import json

from mcp import types

from . import database
from .child_manager import child_manager
from .installer import uninstall
from .models import Server, ServerType


def _cfg(c: Server) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "type": c.type,
        "package": c.package,
        "args": c.get_args(),
        "env": c.get_env(),
        "enabled": c.enabled,
    }


async def _find_by_name(name: str) -> Server:
    for server in await database.list_servers():
        if server.name == name:
            return server
    raise ValueError(f"No server named {name!r}")


async def _list_servers(arguments: dict) -> list[dict]:
    servers = await database.list_servers()
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


async def _add_server(arguments: dict) -> dict:
    name = arguments["name"]
    type_str = arguments["type"]
    package = arguments["package"]
    args = arguments.get("args", [])
    env = arguments.get("env", {})

    server_type = ServerType(type_str)  # raises ValueError for an unknown type

    try:
        config = await database.add_server(name, server_type, package, args, env)
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


async def _delete_server(arguments: dict) -> dict:
    name = arguments["name"]
    server = await _find_by_name(name)
    await child_manager.remove(server.name)
    await uninstall(server)
    await database.delete_server(server.id)
    return {"deleted": name}


async def _enable_server(arguments: dict) -> dict:
    name = arguments["name"]
    server = await _find_by_name(name)
    await database.update_server_enabled(server.id, True)
    server.enabled = True
    try:
        state = await child_manager.add(server)
        return {"name": name, "enabled": True, "tool_count": len(state.tools)}
    except Exception as exc:
        return {"name": name, "enabled": True, "tool_count": 0, "error": str(exc)}


async def _disable_server(arguments: dict) -> dict:
    name = arguments["name"]
    server = await _find_by_name(name)
    await child_manager.remove(server.name)
    await database.update_server_enabled(server.id, False)
    return {"name": name, "enabled": False}


async def _restart_server(arguments: dict) -> dict:
    name = arguments["name"]
    await _find_by_name(name)  # validates existence with a clear error first
    try:
        state = await child_manager.restart(name)
    except KeyError:
        raise ValueError(f"Server {name!r} is not running")
    return {"name": name, "tool_count": len(state.tools)}


_NAME_ONLY_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}

TOOLS: list[types.Tool] = [
    types.Tool(
        name="list_servers",
        description="List all configured MCP servers with their status.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="add_server",
        description="Add and start a new MCP server (pypi/npm/git/cmd).",
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
            },
            "required": ["name", "type", "package"],
        },
    ),
    types.Tool(
        name="delete_server",
        description="Stop and permanently remove a configured MCP server.",
        inputSchema=_NAME_ONLY_SCHEMA,
    ),
    types.Tool(
        name="enable_server",
        description="Enable and start a previously disabled MCP server.",
        inputSchema=_NAME_ONLY_SCHEMA,
    ),
    types.Tool(
        name="disable_server",
        description="Stop and disable an MCP server without deleting it.",
        inputSchema=_NAME_ONLY_SCHEMA,
    ),
    types.Tool(
        name="restart_server",
        description="Restart a currently running MCP server.",
        inputSchema=_NAME_ONLY_SCHEMA,
    ),
]

NAMES: frozenset[str] = frozenset(t.name for t in TOOLS)

_HANDLERS = {
    "list_servers": _list_servers,
    "add_server": _add_server,
    "delete_server": _delete_server,
    "enable_server": _enable_server,
    "disable_server": _disable_server,
    "restart_server": _restart_server,
}


async def call(name: str, arguments: dict) -> list[types.TextContent]:
    result = await _HANDLERS[name](arguments)
    return [types.TextContent(type="text", text=json.dumps(result))]
```

- [ ] **Step 2: Verify the module imports cleanly**

```bash
cd packages/aggregator
uv run python -c "from aggregator import meta_tools; print(sorted(meta_tools.NAMES))"
```

Expected: `['add_server', 'delete_server', 'disable_server', 'enable_server', 'list_servers', 'restart_server']` — no import errors.

- [ ] **Step 3: Manual verification — full add/list/enable/disable/restart/delete round-trip**

Run from `packages/aggregator` (uses a real `pypi`-type server so `child_manager.add()` has something to actually start — swap `mcp-server-fetch` for any small `uvx`-installable MCP server if that one isn't available in your environment):

```bash
uv run python -c "
import asyncio, json
from aggregator import meta_tools

async def main():
    add = await meta_tools.call('add_server', {
        'name': 'verify-meta',
        'type': 'pypi',
        'package': 'mcp-server-fetch',
    })
    print('add_server ->', add[0].text)

    listed = await meta_tools.call('list_servers', {})
    names = [s['name'] for s in json.loads(listed[0].text)]
    assert 'verify-meta' in names, f'verify-meta missing from {names}'
    print('list_servers OK, contains verify-meta')

    off = await meta_tools.call('disable_server', {'name': 'verify-meta'})
    print('disable_server ->', off[0].text)

    on = await meta_tools.call('enable_server', {'name': 'verify-meta'})
    print('enable_server ->', on[0].text)

    restarted = await meta_tools.call('restart_server', {'name': 'verify-meta'})
    print('restart_server ->', restarted[0].text)

    deleted = await meta_tools.call('delete_server', {'name': 'verify-meta'})
    print('delete_server ->', deleted[0].text)

    listed_after = await meta_tools.call('list_servers', {})
    names_after = [s['name'] for s in json.loads(listed_after[0].text)]
    assert 'verify-meta' not in names_after, 'verify-meta still present after delete'
    print('Confirmed removed.')

asyncio.run(main())
"
```

Expected: each `print` line shows a JSON payload with no `Traceback`; `add_server`'s output has `"tools"` containing at least one tool name and no `"error"`; the final assertion passes silently (no `AssertionError`).

- [ ] **Step 4: Manual verification — error paths**

```bash
uv run python -c "
import asyncio
from aggregator import meta_tools

async def main():
    try:
        await meta_tools.call('restart_server', {'name': 'does-not-exist'})
    except ValueError as exc:
        print('restart_server on unknown name raised ValueError:', exc)
    else:
        print('UNEXPECTED: no error raised')

    try:
        await meta_tools.call('add_server', {'name': 'x', 'type': 'not-a-real-type', 'package': 'y'})
    except ValueError as exc:
        print('add_server with bad type raised ValueError:', exc)
    else:
        print('UNEXPECTED: no error raised')

asyncio.run(main())
"
```

Expected: both branches print a `ValueError` message, neither prints `UNEXPECTED`.

- [ ] **Step 5: Commit**

```bash
git add packages/aggregator/src/aggregator/meta_tools.py
git commit -m "feat(aggregator): add native meta tools for server management"
```

---

### Task 2: Wire meta tools into `aggregator.py`

**Files:**
- Modify: `packages/aggregator/src/aggregator/aggregator.py`

**Interfaces:**
- Consumes: `meta_tools.TOOLS`, `meta_tools.NAMES`, `meta_tools.call` (Task 1).
- Produces: nothing new — `handle_list_tools()`/`handle_call_tool()` behavior extended in place; both remain the same functions any future task or the `/mcp` route (`main.py`) already calls.

`aggregator.py` no longer uses decorator-based registration — `mcp==2.0.0` requires `Server(...)` to be constructed with `on_list_tools`/`on_call_tool` callbacks that take `(ctx, params)` and return typed `ListToolsResult`/`CallToolResult` objects. The diffs below target that actual current shape.

- [ ] **Step 1: Add the import**

In `aggregator.py`, find:

```python
from mcp import types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.sse import SseServerTransport

from .child_manager import child_manager
```

Change to:

```python
from mcp import types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.sse import SseServerTransport

from . import meta_tools
from .child_manager import child_manager
```

- [ ] **Step 2: Include meta tools in `handle_list_tools()`**

Find:

```python
async def handle_list_tools(
    _ctx: ServerRequestContext, _params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    tools = []
    for server_name, tool in child_manager.all_tools():
```

Change to:

```python
async def handle_list_tools(
    _ctx: ServerRequestContext, _params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    tools = list(meta_tools.TOOLS)
    for server_name, tool in child_manager.all_tools():
```

(The rest of the function — the loop body and `return types.ListToolsResult(tools=tools)` — is unchanged.)

- [ ] **Step 3: Dispatch meta tool calls in `handle_call_tool()`**

Find:

```python
async def handle_call_tool(
    _ctx: ServerRequestContext, params: types.CallToolRequestParams
) -> types.CallToolResult:
    child, tool_name = child_manager.resolve(params.name)
    if child is None or not child.running:
        raise ValueError(f"No running server found for tool: {params.name!r}")
    result = await child.session.call_tool(tool_name, params.arguments or {})
    return types.CallToolResult(content=result.content, is_error=result.is_error or False)
```

Change to:

```python
async def handle_call_tool(
    _ctx: ServerRequestContext, params: types.CallToolRequestParams
) -> types.CallToolResult:
    if params.name in meta_tools.NAMES:
        content = await meta_tools.call(params.name, params.arguments or {})
        return types.CallToolResult(content=content, is_error=False)

    child, tool_name = child_manager.resolve(params.name)
    if child is None or not child.running:
        raise ValueError(f"No running server found for tool: {params.name!r}")
    result = await child.session.call_tool(tool_name, params.arguments or {})
    return types.CallToolResult(content=result.content, is_error=result.is_error or False)
```

`mcp_server = Server("mcp-aggregator", on_list_tools=handle_list_tools, on_call_tool=handle_call_tool)`, further down the file, references these two functions by name and needs no change — it already picks up the new behavior since the functions themselves are what changed.

- [ ] **Step 4: Verify the module imports and the tool list includes the meta tools**

```bash
cd packages/aggregator
uv run python -c "
import asyncio
from aggregator.aggregator import handle_list_tools

async def main():
    result = await handle_list_tools(None, None)
    names = sorted(t.name for t in result.tools)
    print(names)
    for expected in ('list_servers', 'add_server', 'delete_server', 'enable_server', 'disable_server', 'restart_server'):
        assert expected in names, f'{expected} missing from tool list'
    print('All meta tools present.')

asyncio.run(main())
"
```

Expected: a printed list of tool names including all six meta tools (plus any currently-running proxied `<server>__<tool>` entries), then `All meta tools present.` with no `AssertionError`/`Traceback`. (Passing `None` for `_ctx` is safe here — `handle_list_tools` never touches its first argument.)

- [ ] **Step 5: Commit**

```bash
git add packages/aggregator/src/aggregator/aggregator.py
git commit -m "feat(aggregator): dispatch native meta tools alongside proxied tools"
```

---

### Task 3: End-to-end verification via a live `/mcp` connection

**Files:** none — verification only, no committed deliverable.

**Interfaces:**
- Consumes: `/mcp` (existing SSE endpoint, `main.py`), `ADMIN_TOKEN` bearer auth (existing, `.env`), the six meta tools wired in Task 2.

- [ ] **Step 1: Start the aggregator and connect a real MCP client**

```bash
just up      # or: uv run uvicorn aggregator.main:app --reload   (from packages/aggregator)
npx @modelcontextprotocol/inspector
```

In the Inspector UI: URL `http://localhost:8000/mcp`, header `Authorization: Bearer <ADMIN_TOKEN>` (`TOKEN=$(grep ^ADMIN_TOKEN .env | cut -d= -f2)` per the README's existing local-testing instructions).

- [ ] **Step 2: Confirm the meta tools are listed**

In the Inspector's tool list, confirm `list_servers`, `add_server`, `delete_server`, `enable_server`, `disable_server`, `restart_server` are all present, unprefixed (no `__` in the name), alongside any `<server>__<tool>` proxied entries.

- [ ] **Step 3: Exercise the full round-trip through the Inspector**

Call `add_server` with `{"name": "verify-meta", "type": "pypi", "package": "mcp-server-fetch"}`, then `list_servers` and confirm `verify-meta` appears with `"running": true`. Call `disable_server`, then `enable_server`, then `restart_server`, each with `{"name": "verify-meta"}`, confirming each response matches what Task 1 Step 3's scripted check already validated in isolation. Call `delete_server` with `{"name": "verify-meta"}` and confirm a follow-up `list_servers` no longer includes it.

Expected: every call returns a JSON payload (not a protocol-level error), and the sequence of results matches Task 1 Step 3's already-verified behavior — this step's purpose is confirming the `/mcp` wiring from Task 2, not re-testing `meta_tools.py`'s own logic again.

No commit — this task validates Tasks 1-2 together; if anything here doesn't match, the bug is in Task 1 or Task 2 and should be fixed there.
