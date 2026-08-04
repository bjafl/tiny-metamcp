# Edit MCP Server Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an existing MCP server config (`name`, `type`, `package`, `args`, `env`) be edited in place — via REST API, MCP meta-tool, and the admin webui — instead of requiring delete-and-re-add.

**Architecture:** A single new DB function (`database.update_server`) does a partial field update (only fields passed in are changed; `env`/`args` are replaced wholesale, not deep-merged). Both the REST `PATCH /servers/{id}` endpoint and the new `edit_server` meta-tool call it, then apply the change to the running child by removing the old `ChildState` (if any) and re-adding under the new config — the same stop/start primitives `add_server`/`enable_server` already use. No rollback on start failure: the edit is kept, the error is surfaced in the response, matching how `add_server`/`restart` already behave. The webui reuses `AddServerDialog` in a controlled "edit mode" rather than adding a new component.

**Tech Stack:** Python 3.14 / FastAPI / SQLModel+aiosqlite (aggregator), pytest + pytest-asyncio (`asyncio_mode = "auto"`), React 19 + TanStack Query (webui), TypeScript.

## Global Constraints

- Run aggregator Python commands as `cd packages/aggregator && uv run ...` — plain `uv run` from the repo root fails (`packages/webui` has no `pyproject.toml`).
- `just test` runs the aggregator's pytest suite; `just lint` / `just format` run ruff via `uvx` (not a project dependency).
- Tests must use a real local MCP server, not a mocked transport — use the session-scoped `proxy_target_url` fixture in `packages/aggregator/tests/conftest.py` (a real Streamable HTTP MCP server with `echo`/`add` tools) for anything that needs a running child.
- Every test that adds a child to `child_manager` must remove it again (in a `finally`/cleanup step) — `tests/conftest.py`'s autouse `_clean_child_manager` fixture asserts `child_manager._children` is empty before *and* after every test.
- `mcp` is pinned `>=2,<3` on purpose (SDK 2.0.0 API). Don't touch that pin.
- Frontend: no test framework is configured for `packages/webui` (no vitest/jest) — verify frontend changes with `pnpm --filter webui build` (runs `tsc -b`, i.e. typecheck) and `pnpm --filter webui lint` (oxlint), plus a manual check in the dev server (`just webui-dev`, proxies `/api` to `:8000`).

---

### Task 1: Database layer — `update_server()`

**Files:**
- Modify: `packages/aggregator/src/aggregator/database.py:61-67` (insert a new function after `update_server_enabled`, before `delete_server`)
- Test: Create `packages/aggregator/tests/test_database.py`

**Interfaces:**
- Consumes: `aggregator.database.add_server(name, server_type, package, args=None, env=None) -> Server` (existing, `database.py:40-58`), `aggregator.database.delete_server(server_id) -> None` (existing, for test cleanup), `aggregator.models.Server`, `aggregator.models.ServerType`.
- Produces: `aggregator.database.update_server(server_id: int, name: str | None = None, server_type: ServerType | None = None, package: str | None = None, args: list[str] | None = None, env: dict[str, str] | None = None) -> Server | None`. Returns `None` if `server_id` doesn't exist. Any other field left `None` keeps its current DB value; `args`/`env` are replaced wholesale when provided (no per-key merge). Tasks 2 and 3 call this directly.

- [ ] **Step 1: Write the failing tests**

Create `packages/aggregator/tests/test_database.py`:

```python
"""
Regression tests for aggregator.database.update_server -- the partial-field
update used by both PATCH /servers/{id} and the edit_server meta-tool.
"""

import pytest

from aggregator.database import add_server, delete_server, update_server
from aggregator.models import ServerType


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
        "edit-db-wholesale", ServerType.PROXY, "http://example.invalid/mcp",
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
        with pytest.raises(Exception):
            await update_server(b.id, name="edit-db-conflict-a")
    finally:
        await _cleanup(a.id)
        await _cleanup(b.id)


async def test_update_server_unknown_id_returns_none():
    assert await update_server(999_999_999, name="whatever") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_database.py -v`
Expected: FAIL — `ImportError: cannot import name 'update_server'` (the function doesn't exist yet).

- [ ] **Step 3: Implement `update_server`**

In `packages/aggregator/src/aggregator/database.py`, insert after `update_server_enabled` (currently ending at line 66) and before `delete_server`:

```python
async def update_server(
    server_id: int,
    name: str | None = None,
    server_type: ServerType | None = None,
    package: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> Server | None:
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
        await session.commit()
        await session.refresh(server)
        return server
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_database.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Lint and commit**

Run: `cd packages/aggregator && uvx ruff check src tests && uvx ruff format --check src tests`
Expected: no errors (if ruff reformats anything, re-run `uvx ruff format src tests` and re-check).

```bash
git add packages/aggregator/src/aggregator/database.py packages/aggregator/tests/test_database.py
git commit -m "feat(aggregator): add database.update_server for partial config edits"
```

---

### Task 2: REST API — `PATCH /servers/{id}`

**Files:**
- Modify: `packages/aggregator/src/aggregator/api/routers.py:9-15` (import), `:25-30` (add `ServerUpdateRequest` after `AddServerRequest`), insert new route after `api_add_server` (currently ending line 63), before `api_delete_server`.
- Test: Create `packages/aggregator/tests/test_routers.py`

**Interfaces:**
- Consumes: `aggregator.database.update_server(...)` (Task 1), `aggregator.database.get_server(server_id) -> Server | None` (existing, `database.py:35-37`), `aggregator.child_manager.child_manager` singleton — `.get(name) -> ChildState | None`, `.remove(name) -> None`, `.add(config) -> ChildState` (existing, `child_manager.py`), `routers.py`'s own `_cfg(c: Server) -> dict` helper (`routers.py:188-197`).
- Produces: `PATCH /api/servers/{server_id}` — request body `ServerUpdateRequest` (all fields optional: `name`, `type`, `package`, `args`, `env`); response `{**_cfg(config), "running": bool, "tool_count": int, "error": str | None}`. 404 if the id doesn't exist, 400 if the update itself fails (e.g. name conflict). Task 4 (webui) calls this endpoint.

- [ ] **Step 1: Write the failing tests**

Create `packages/aggregator/tests/test_routers.py`:

```python
"""
Regression tests for the /api/servers REST routes added for editing --
PATCH /servers/{id}. Uses a minimal FastAPI app (just api_router, no
lifespan) over httpx's ASGI transport, matching the auth conventions
require_api_auth expects (Bearer ADMIN_TOKEN, set to "test-admin-token"
by tests/conftest.py before aggregator.config is first imported).
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from aggregator.api.routers import router as api_router
from aggregator.child_manager import child_manager
from aggregator.database import delete_server, list_servers

AUTH_HEADERS = {"Authorization": "Bearer test-admin-token"}


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _cleanup_by_name(name: str) -> None:
    if child_manager.get(name):
        await child_manager.remove(name)
    for server in await list_servers():
        if server.name == name:
            await delete_server(server.id)


async def test_patch_updates_only_provided_fields(client):
    name = "patch-partial"
    try:
        added = await client.post(
            "/api/servers",
            json={"name": name, "type": "proxy", "package": "http://a.invalid/mcp", "env": {"A": "1"}},
            headers=AUTH_HEADERS,
        )
        server_id = added.json()["server"]["id"]

        resp = await client.patch(
            f"/api/servers/{server_id}", json={"env": {"B": "2"}}, headers=AUTH_HEADERS
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == name
        assert body["package"] == "http://a.invalid/mcp"
        assert body["env"] == {"B": "2"}
    finally:
        await _cleanup_by_name(name)


async def test_patch_rename_conflict_returns_400(client):
    name_a, name_b = "patch-conflict-a", "patch-conflict-b"
    try:
        a = await client.post(
            "/api/servers",
            json={"name": name_a, "type": "proxy", "package": "http://a.invalid/mcp"},
            headers=AUTH_HEADERS,
        )
        b = await client.post(
            "/api/servers",
            json={"name": name_b, "type": "proxy", "package": "http://b.invalid/mcp"},
            headers=AUTH_HEADERS,
        )
        b_id = b.json()["server"]["id"]

        resp = await client.patch(
            f"/api/servers/{b_id}", json={"name": name_a}, headers=AUTH_HEADERS
        )
        assert resp.status_code == 400
    finally:
        await _cleanup_by_name(name_a)
        await _cleanup_by_name(name_b)


async def test_patch_while_running_restarts_child_under_new_name(client, proxy_target_url):
    old_name, new_name = "patch-running-old", "patch-running-new"
    try:
        added = await client.post(
            "/api/servers",
            json={"name": old_name, "type": "proxy", "package": proxy_target_url},
            headers=AUTH_HEADERS,
        )
        server_id = added.json()["server"]["id"]
        assert child_manager.get(old_name) is not None

        resp = await client.patch(
            f"/api/servers/{server_id}", json={"name": new_name}, headers=AUTH_HEADERS
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


async def test_patch_while_disabled_does_not_touch_child_manager(client):
    name = "patch-disabled"
    try:
        added = await client.post(
            "/api/servers",
            json={"name": name, "type": "proxy", "package": "http://a.invalid/mcp"},
            headers=AUTH_HEADERS,
        )
        server_id = added.json()["server"]["id"]
        await client.post(f"/api/servers/{server_id}/disable", headers=AUTH_HEADERS)
        assert child_manager.get(name) is None

        resp = await client.patch(
            f"/api/servers/{server_id}", json={"package": "http://b.invalid/mcp"}, headers=AUTH_HEADERS
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is False
        assert body["package"] == "http://b.invalid/mcp"
        assert child_manager.get(name) is None
    finally:
        await _cleanup_by_name(name)


async def test_patch_nonexistent_id_returns_404(client):
    resp = await client.patch(
        "/api/servers/999999999", json={"package": "http://x.invalid/mcp"}, headers=AUTH_HEADERS
    )
    assert resp.status_code == 404


async def test_patch_without_auth_returns_401(client):
    resp = await client.patch("/api/servers/1", json={"package": "http://x.invalid/mcp"})
    assert resp.status_code == 401
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_routers.py -v`
Expected: FAIL — `404`/`405 Method Not Allowed` on the `client.patch(...)` calls (no `PATCH /servers/{id}` route exists yet).

- [ ] **Step 3: Implement the route**

In `packages/aggregator/src/aggregator/api/routers.py`, change the import block (lines 9-15) to also import `update_server`:

```python
from ..database import (
    add_server,
    delete_server,
    get_server,
    list_servers,
    update_server,
    update_server_enabled,
)
```

Add `ServerUpdateRequest` right after `AddServerRequest` (after line 30):

```python
class ServerUpdateRequest(BaseModel):
    name: str | None = None
    type: ServerType | None = None
    package: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
```

Add the route after `api_add_server` (after line 63), before `api_delete_server`:

```python
@router.patch("/servers/{server_id}")
async def api_update_server(server_id: int, req: ServerUpdateRequest):
    existing = await get_server(server_id)
    if not existing:
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
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if was_running:
        await child_manager.remove(existing.name)

    if not config.enabled:
        return {**_cfg(config), "running": False, "tool_count": 0, "error": None}

    try:
        state = await child_manager.add(config)
        return {**_cfg(config), "running": True, "tool_count": len(state.tools), "error": None}
    except Exception as exc:
        return {**_cfg(config), "running": False, "tool_count": 0, "error": str(exc)}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_routers.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Run the full aggregator suite, lint, and commit**

Run: `cd packages/aggregator && uv run pytest && uvx ruff check src tests && uvx ruff format --check src tests`
Expected: all tests pass (including Task 1's and the pre-existing suites), no lint errors.

```bash
git add packages/aggregator/src/aggregator/api/routers.py packages/aggregator/tests/test_routers.py
git commit -m "feat(aggregator): add PATCH /servers/{id} for editing a server config"
```

---

### Task 3: MCP meta-tool — `edit_server`

**Files:**
- Modify: `packages/aggregator/src/aggregator/meta_tools.py:54-77` (insert `_edit_server` after `_add_server`), `:124-169` (add to `TOOLS`), `:173-180` (add to `_HANDLERS`)
- Test: Modify `packages/aggregator/tests/test_meta_tools.py`

**Interfaces:**
- Consumes: `aggregator.database.update_server(...)` (Task 1), `aggregator.database.Server` (via `_find_by_name`, existing `meta_tools.py:33-37`), `aggregator.child_manager.child_manager`, `meta_tools.py`'s own `_cfg(c: Server) -> dict` (env-redacting variant, `meta_tools.py:19-30`).
- Produces: `edit_server` MCP tool, callable as `meta_tools.call("edit_server", {"name": ..., "new_name"?: ..., "type"?: ..., "package"?: ..., "args"?: ..., "env"?: ...})` returning `{"server": {...redacted cfg...}, "tools": [str], "error": str | None}`. `name` identifies the server to edit (matching every other meta-tool's identify-by-name convention); `new_name` is the optional new name — distinct from `name` on purpose, since `name` is already "the target" in `delete_server`/`enable_server`/`disable_server`/`restart_server`.

- [ ] **Step 1: Write the failing tests**

Append to `packages/aggregator/tests/test_meta_tools.py` (after the existing `test_add_server_with_invalid_type_raises_value_error`, reusing that file's `_payload`/`_cleanup_by_name` helpers already defined at the top):

```python
async def test_edit_server_updates_only_provided_fields(proxy_target_url):
    name = "meta-edit-partial"
    try:
        await meta_tools.call(
            "add_server",
            {"name": name, "type": "proxy", "package": proxy_target_url, "env": {"A": "1"}},
        )
        edited = _payload(
            await meta_tools.call("edit_server", {"name": name, "env": {"B": "2"}})
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
            "add_server", {"name": old_name, "type": "proxy", "package": proxy_target_url}
        )
        assert child_manager.get(old_name) is not None

        edited = _payload(
            await meta_tools.call("edit_server", {"name": old_name, "new_name": new_name})
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
        await meta_tools.call("edit_server", {"name": "does-not-exist", "package": "x"})


async def test_edit_server_invalid_type_raises_value_error(proxy_target_url):
    name = "meta-edit-bad-type"
    try:
        await meta_tools.call(
            "add_server", {"name": name, "type": "proxy", "package": proxy_target_url}
        )
        with pytest.raises(ValueError):
            await meta_tools.call("edit_server", {"name": name, "type": "not-a-real-type"})
    finally:
        await _cleanup_by_name(name)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd packages/aggregator && uv run pytest tests/test_meta_tools.py -v -k edit_server`
Expected: FAIL — `KeyError: 'edit_server'` from `_HANDLERS[name]` in `meta_tools.call` (the tool doesn't exist yet).

- [ ] **Step 3: Implement `_edit_server` and register it**

In `packages/aggregator/src/aggregator/meta_tools.py`, insert after `_add_server` (after line 77), before `_delete_server`:

```python
async def _edit_server(arguments: dict) -> dict:
    name = arguments["name"]
    server = await _find_by_name(name)

    type_str = arguments.get("type")
    server_type = ServerType(type_str) if type_str is not None else None  # raises ValueError

    was_running = child_manager.get(server.name) is not None
    try:
        config = await database.update_server(
            server.id,
            name=arguments.get("new_name"),
            server_type=server_type,
            package=arguments.get("package"),
            args=arguments.get("args"),
            env=arguments.get("env"),
        )
    except Exception as exc:
        raise ValueError(str(exc)) from exc

    if was_running:
        await child_manager.remove(server.name)

    if not config.enabled:
        return {"server": _cfg(config), "tools": [], "error": None}

    try:
        state = await child_manager.add(config)
        return {"server": _cfg(config), "tools": [t.name for t in state.tools], "error": None}
    except Exception as exc:
        return {"server": _cfg(config), "tools": [], "error": str(exc)}
```

Add the tool definition to `TOOLS` (insert after the `add_server` entry, currently ending at line 148, before `delete_server`):

```python
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
                "type": {"type": "string"},
                "package": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["name"],
        },
    ),
```

Register the handler in `_HANDLERS` (line 173-180):

```python
_HANDLERS = {
    "list_servers": _list_servers,
    "add_server": _add_server,
    "edit_server": _edit_server,
    "delete_server": _delete_server,
    "enable_server": _enable_server,
    "disable_server": _disable_server,
    "restart_server": _restart_server,
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd packages/aggregator && uv run pytest tests/test_meta_tools.py -v`
Expected: PASS (all tests in the file, including the 4 new ones).

- [ ] **Step 5: Run the full aggregator suite, lint, and commit**

Run: `cd packages/aggregator && uv run pytest && uvx ruff check src tests && uvx ruff format --check src tests`
Expected: all tests pass, no lint errors.

```bash
git add packages/aggregator/src/aggregator/meta_tools.py packages/aggregator/tests/test_meta_tools.py
git commit -m "feat(aggregator): add edit_server MCP meta-tool"
```

---

### Task 4: Admin webui — edit mode for `AddServerDialog`

**Files:**
- Modify: `packages/webui/src/lib/api.ts:39-68` (add `editServer`)
- Modify: `packages/webui/src/hooks/useServers.ts` (add `useEditServer`)
- Modify: `packages/webui/src/components/AddServerDialog.tsx` (accept optional `server`/`open`/`onOpenChange` props, prefill, branch submit)
- Modify: `packages/webui/src/components/ServerTable.tsx` (add "Edit" row action + one shared controlled dialog instance)

**Interfaces:**
- Consumes: `PATCH /api/servers/{id}` (Task 2), existing `api.request<T>()` helper (`api.ts:19-37`), existing `ServerConfig`/`AddServerInput` types (`types.ts:3-22`), existing `useAddServer()` pattern (`useServers.ts:15-21`).
- Produces: `api.editServer(id: number, input: AddServerInput): Promise<ServerConfig>`; `useEditServer(): UseMutationResult<ServerConfig, ApiError, { id: number; input: AddServerInput }>`; `<AddServerDialog server?: ServerConfig, open?: boolean, onOpenChange?: (open: boolean) => void>` — when `server` is provided the dialog runs in controlled edit mode (no built-in trigger button, prefilled fields, submits via `useEditServer`); when omitted it behaves exactly as before (self-contained "Add server" button + `useAddServer`).

- [ ] **Step 1: Add `editServer` to the API client**

In `packages/webui/src/lib/api.ts`, add to the `api` object (after `addServer`, before `deleteServer`, i.e. after line 46):

```typescript
  editServer: (id: number, input: AddServerInput) =>
    request<ServerConfig>(`/api/servers/${id}`, {
      method: "PATCH",
      body: JSON.stringify(input),
    }),
```

`ServerConfig` is already imported at the top of the file (line 7) — no import changes needed.

- [ ] **Step 2: Add `useEditServer` hook**

In `packages/webui/src/hooks/useServers.ts`, add after `useAddServer` (after line 21):

```typescript
export function useEditServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, input }: { id: number; input: AddServerInput }) =>
      api.editServer(id, input),
    onSuccess: () => qc.invalidateQueries({ queryKey: serversKey }),
  });
}
```

- [ ] **Step 3: Add edit mode to `AddServerDialog`**

Replace the full contents of `packages/webui/src/components/AddServerDialog.tsx`:

```tsx
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
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
import { useAddServer, useEditServer } from "@/hooks/useServers";
import type { ServerConfig, ServerType } from "@/lib/types";

function parseArgs(raw: string): string[] {
  return raw
    .split(",")
    .map((a) => a.trim())
    .filter(Boolean);
}

function parseEnv(raw: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const pair of raw.split(",")) {
    const [k, ...rest] = pair.split("=");
    if (k && rest.length) env[k.trim()] = rest.join("=").trim();
  }
  return env;
}

function formatArgs(args: string[]): string {
  return args.join(", ");
}

function formatEnv(env: Record<string, string>): string {
  return Object.entries(env)
    .map(([k, v]) => `${k}=${v}`)
    .join(", ");
}

interface AddServerDialogProps {
  server?: ServerConfig;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

export function AddServerDialog({
  server,
  open: openProp,
  onOpenChange,
}: AddServerDialogProps = {}) {
  const isEdit = server != null;
  const [internalOpen, setInternalOpen] = useState(false);
  const open = isEdit ? (openProp ?? false) : internalOpen;
  const setOpen = isEdit ? (onOpenChange ?? (() => {})) : setInternalOpen;

  const [name, setName] = useState("");
  const [type, setType] = useState<ServerType>("pypi");
  const [pkg, setPkg] = useState("");
  const [args, setArgs] = useState("");
  const [env, setEnv] = useState("");
  const addServer = useAddServer();
  const editServer = useEditServer();
  const mutation = isEdit ? editServer : addServer;

  useEffect(() => {
    if (!open) return;
    setName(server?.name ?? "");
    setType(server?.type ?? "pypi");
    setPkg(server?.package ?? "");
    setArgs(server ? formatArgs(server.args) : "");
    setEnv(server ? formatEnv(server.env) : "");
  }, [open, server]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const payload = { name, type, package: pkg, args: parseArgs(args), env: parseEnv(env) };
    if (isEdit && server) {
      await editServer.mutateAsync({ id: server.id, input: payload });
    } else {
      await addServer.mutateAsync(payload);
    }
    setOpen(false);
    if (!isEdit) {
      setName("");
      setPkg("");
      setArgs("");
      setEnv("");
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {!isEdit ? (
        <DialogTrigger asChild>
          <Button>Add server</Button>
        </DialogTrigger>
      ) : null}
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isEdit ? "Edit server" : "Add server"}</DialogTitle>
        </DialogHeader>
        <form className="space-y-4" onSubmit={handleSubmit}>
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1">
              <Label htmlFor="name">Name</Label>
              <Input
                id="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
            </div>
            <div className="space-y-1">
              <Label>Type</Label>
              <Select value={type} onValueChange={(v) => setType(v as ServerType)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="pypi">PyPI (uvx)</SelectItem>
                  <SelectItem value="npm">npm (npx)</SelectItem>
                  <SelectItem value="git">Git repo</SelectItem>
                  <SelectItem value="cmd">Command</SelectItem>
                  <SelectItem value="proxy">Proxy (remote URL)</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <div className="space-y-1">
            <Label htmlFor="package">Package / source</Label>
            <Input
              id="package"
              value={pkg}
              onChange={(e) => setPkg(e.target.value)}
              placeholder="mcp-server-fetch or git+https://... or /usr/bin/cmd or http://host:port/mcp"
              required
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1">
              <Label htmlFor="args">Args (comma-separated)</Label>
              <Input id="args" value={args} onChange={(e) => setArgs(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="env">Env (KEY=VALUE, comma-separated)</Label>
              <Input id="env" value={env} onChange={(e) => setEnv(e.target.value)} />
            </div>
          </div>
          {type === "proxy" ? (
            <p className="text-xs text-muted-foreground">
              Args and env are ignored for the proxy type — it connects to an
              already-running server, nothing gets launched locally.
            </p>
          ) : null}
          {mutation.isError ? (
            <p className="text-sm text-destructive">{mutation.error.message}</p>
          ) : null}
          {mutation.data?.error ? (
            <p className="text-sm text-destructive">
              Started with error: {mutation.data.error}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="submit" disabled={mutation.isPending}>
              {mutation.isPending
                ? isEdit
                  ? "Saving…"
                  : "Installing…"
                : isEdit
                  ? "Save changes"
                  : "Add server"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 4: Add the "Edit" row action to `ServerTable`**

Replace the full contents of `packages/webui/src/components/ServerTable.tsx`:

```tsx
import { useState } from "react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/StatusBadge";
import { AddServerDialog } from "@/components/AddServerDialog";
import {
  useDeleteServer,
  useDisableServer,
  useEnableServer,
  useRestartServer,
} from "@/hooks/useServers";
import type { ServerConfig } from "@/lib/types";

export function ServerTable({ servers }: { servers: ServerConfig[] }) {
  const enableServer = useEnableServer();
  const disableServer = useDisableServer();
  const restartServer = useRestartServer();
  const deleteServer = useDeleteServer();
  const [editingServer, setEditingServer] = useState<ServerConfig | null>(null);

  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Name</TableHead>
            <TableHead>Type</TableHead>
            <TableHead>Package</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {servers.map((s) => (
            <TableRow key={s.id}>
              <TableCell className="font-medium">{s.name}</TableCell>
              <TableCell>{s.type}</TableCell>
              <TableCell className="max-w-xs truncate">{s.package}</TableCell>
              <TableCell>
                <StatusBadge server={s} />
                {s.error ? (
                  <p className="mt-1 text-xs text-destructive">{s.error}</p>
                ) : null}
              </TableCell>
              <TableCell className="space-x-2 text-right">
                {s.enabled ? (
                  <>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => restartServer.mutate(s.id)}
                    >
                      Restart
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => disableServer.mutate(s.id)}
                    >
                      Disable
                    </Button>
                  </>
                ) : (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => enableServer.mutate(s.id)}
                  >
                    Enable
                  </Button>
                )}
                <Button size="sm" variant="outline" onClick={() => setEditingServer(s)}>
                  Edit
                </Button>
                <Button
                  size="sm"
                  variant="destructive"
                  onClick={() => {
                    if (confirm(`Delete server "${s.name}"?`)) {
                      deleteServer.mutate(s.id);
                    }
                  }}
                >
                  Delete
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <AddServerDialog
        server={editingServer ?? undefined}
        open={editingServer !== null}
        onOpenChange={(o) => {
          if (!o) setEditingServer(null);
        }}
      />
    </>
  );
}
```

- [ ] **Step 5: Typecheck, lint, and manually verify in the browser**

Run: `pnpm --filter webui build`
Expected: succeeds — `tsc -b` reports no type errors (in particular: `AddServerDialogProps` matches how `ServersPage.tsx` still calls `<AddServerDialog />` with no props, and how `ServerTable.tsx` now calls it with `server`/`open`/`onOpenChange`).

Run: `pnpm --filter webui lint`
Expected: no errors.

Then start the stack and manually verify the golden path and edge cases in a browser:

Run: `just webui-dev` (in one terminal) and separately ensure the aggregator backend is running (e.g. `just dev` or an existing `just up` deployment) so `/api` calls resolve.

In the browser:
1. Open the Servers page, click "Edit" on an existing server — dialog opens titled "Edit server", fields prefilled with its current `name`/`type`/`package`/`args`/`env`.
2. Change one field (e.g. an env var) and save — dialog closes, table row reflects the change, no page reload needed (query invalidation).
3. Rename a running server to a name that already exists — expect the dialog to surface the 400 error message inline (via `mutation.isError`) and stay open.
4. Confirm the original "Add server" flow (top-right button) still works unchanged.

- [ ] **Step 6: Commit**

```bash
git add packages/webui/src/lib/api.ts packages/webui/src/hooks/useServers.ts \
  packages/webui/src/components/AddServerDialog.tsx packages/webui/src/components/ServerTable.tsx
git commit -m "feat(webui): add edit mode to AddServerDialog and wire it into ServerTable"
```
