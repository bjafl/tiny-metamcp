# Proxy MCP Server Type Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fifth `Server` type, `proxy`, that connects the aggregator to an already-running MCP server elsewhere on the network via Streamable HTTP, instead of spawning a local subprocess.

**Architecture:** `ChildState.start()` in `child_manager.py` currently only knows how to launch a subprocess via `stdio_client` and reads/writes over its stdio pipes. This plan adds a second branch that, for `type == "proxy"`, opens a Streamable HTTP connection to a URL (stored in the existing `Server.package` field) instead, using `mcp.client.streamable_http.streamable_http_client`. Everything downstream of obtaining `(read, write)` streams — `ClientSession`, tool listing, tool calls, teardown — is shared code, unchanged. The webui gets the new type added to its `ServerType` union and its "Add server" dropdown.

**Tech Stack:** Python (FastAPI, `mcp` SDK, SQLModel), TypeScript/React (Vite, shadcn/ui, TanStack Query).

## Global Constraints

- **Prerequisite satisfied:** the monorepo/webui restructure has landed on `main` (merge commit `712974a`). File paths below (`packages/aggregator/src/aggregator/*`, `packages/webui/*`) are real and current as of this plan's last revision.
- **mcp SDK version confirmed:** `mcp==2.0.0` is pinned and installed (`uv.lock`). Its `mcp.server.Server` API break (decorator-based `@server.list_tools()`/`@server.call_tool()` → constructor-injected `on_list_tools`/`on_call_tool` callbacks) has already been fixed on `main` (commit `56f9d00`) and is unrelated to this plan — `child_manager.py` (what this plan touches) uses the client-side `mcp.client.*` APIs, a separate subsystem the `Server`-class break didn't affect. Directly verified against the installed `mcp==2.0.0`: `mcp.client.streamable_http.streamable_http_client(url, *, http_client=None, terminate_on_close=True)` yields a **2-tuple** `(read_stream, write_stream)` — same shape as `stdio_client`. Task 1 Step 3 below uses this directly; no version-detection step is needed.
- **No auth support for the `proxy` type in this iteration.** The target network (local Docker network) is the trust boundary.
- **No retry/backoff for proxy connections.** A failed connection surfaces as `ChildState.error`, exactly like a bad `pypi` package name does today.
- **No automated test suite exists in this project** (no pytest, no frontend test runner configured by the other plan either). Verification in this plan is manual/scripted (concrete commands and throwaway scripts, run and observed by the engineer) rather than a committed pytest/vitest suite — this matches the project's current testing posture and the spec's explicit decision, not an oversight.

---

### Task 1: Backend — `proxy` server type in `child_manager.py`

**Files:**
- Modify: `packages/aggregator/src/aggregator/models.py` — `ServerType` enum, lines 9-13
- Modify: `packages/aggregator/src/aggregator/child_manager.py` — imports (lines 1-13) and `ChildState.start()` (lines 36-71)

**Interfaces:**
- Consumes: `Server.package` (existing field, `models.py`) — reused to hold the target URL for `proxy` configs, no schema change.
- Produces: `ServerType.PROXY = "proxy"` (consumed by Task 2's webui union and by `api/routers.py`'s existing `AddServerRequest.type: ServerType` validation — no change needed there, it already accepts any enum member). `ChildState.start()` handling `type == ServerType.PROXY` — consumed by Task 3's manual verification.

- [ ] **Step 1: Sanity-check `streamable_http_client` is importable**

Run from `packages/aggregator` (this is a quick smoke check, not a discovery step — the return shape is already confirmed above):

```bash
uv run python -c "
from mcp.client.streamable_http import streamable_http_client
print('OK:', streamable_http_client)
"
```

Expected: `OK: <function streamable_http_client at ...>`, no `ImportError`.

- [ ] **Step 2: Add the `PROXY` server type**

In `models.py`, find:

```python
class ServerType(str, Enum):
    PYPI = "pypi"
    NPM = "npm"
    GIT = "git"
    CMD = "cmd"
```

Change to:

```python
class ServerType(str, Enum):
    PYPI = "pypi"
    NPM = "npm"
    GIT = "git"
    CMD = "cmd"
    PROXY = "proxy"
```

- [ ] **Step 3: Branch `ChildState.start()` on the proxy type**

In `child_manager.py`, add the import (alongside the existing `from mcp.client.stdio import StdioServerParameters, stdio_client` line):

```python
from mcp.client.streamable_http import streamable_http_client
```

And change the existing `from .models import Server` line to:

```python
from .models import Server, ServerType
```

Replace the full `start()` method:

```python
    async def start(self) -> None:
        await install(self.config)
        cmd = build_command(self.config)
        clog = _child_logger(self.config.name)
        clog.info("Starting: %s", " ".join(cmd))

        # Open per-child stderr log file
        self._log_fh = log_capture.open_log_file(self.config.name)

        params = StdioServerParameters(
            command=cmd[0],
            args=cmd[1:],
            env=self.config.get_env() or None,
        )

        stack = AsyncExitStack()
        try:
            # Redirect child stderr to the per-child log file.
            read, write = await stack.enter_async_context(
                stdio_client(params, errlog=self._log_fh)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            result = await session.list_tools()
            self.session = session
            self.tools = result.tools
            self._stack = stack
            self.error = None
            clog.info("Started – %d tool(s): %s",
                      len(self.tools), [t.name for t in self.tools])
        except Exception as exc:
            await stack.aclose()
            self._close_log_fh()
            self.error = str(exc)
            clog.error("Failed to start: %s", exc)
            raise
```

with:

```python
    async def start(self) -> None:
        clog = _child_logger(self.config.name)

        if self.config.type == ServerType.PROXY:
            await self._connect(clog, streamable_http_client(self.config.package))
            return

        await install(self.config)
        cmd = build_command(self.config)
        clog.info("Starting: %s", " ".join(cmd))

        # Open per-child stderr log file
        self._log_fh = log_capture.open_log_file(self.config.name)

        params = StdioServerParameters(
            command=cmd[0],
            args=cmd[1:],
            env=self.config.get_env() or None,
        )
        # Redirect child stderr to the per-child log file.
        await self._connect(clog, stdio_client(params, errlog=self._log_fh))

    async def _connect(self, clog: logging.LoggerAdapter, transport_cm) -> None:
        """Shared session bring-up for both the stdio and proxy transports."""
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(transport_cm)
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            result = await session.list_tools()
            self.session = session
            self.tools = result.tools
            self._stack = stack
            self.error = None
            clog.info("Started – %d tool(s): %s",
                      len(self.tools), [t.name for t in self.tools])
        except Exception as exc:
            await stack.aclose()
            self._close_log_fh()
            self.error = str(exc)
            clog.error("Failed to start: %s", exc)
            raise
```

Both `stdio_client(...)` and `streamable_http_client(...)` yield a plain 2-tuple `(read, write)` under the installed `mcp==2.0.0` (confirmed in the Global Constraints), so `_connect()` can unpack directly — no index-based workaround needed.

- [ ] **Step 4: Manual verification — proxy config bypasses the subprocess path**

Run from `packages/aggregator`:

```bash
uv run python -c "
import asyncio
from aggregator.models import Server, ServerType
from aggregator.child_manager import ChildState

async def main():
    cfg = Server(name='bad-proxy', type=ServerType.PROXY, package='http://127.0.0.1:1/mcp')
    state = ChildState(config=cfg)
    try:
        await state.start()
    except Exception as exc:
        print('Got expected connection error (not a build_command/ValueError):', repr(exc))
    else:
        print('UNEXPECTED: start() succeeded against a closed port')

asyncio.run(main())
"
```

Expected: prints `Got expected connection error: ...` with a connection-refused-style error (e.g. `httpx.ConnectError`), **not** `ValueError: Unknown server type: proxy` (which is what you'd see if `build_command()` were still being called for `proxy`).

- [ ] **Step 5: Commit**

```bash
git add packages/aggregator/src/aggregator/models.py packages/aggregator/src/aggregator/child_manager.py
git commit -m "feat(aggregator): add proxy server type (Streamable HTTP passthrough)"
```

---

### Task 2: Frontend — `proxy` in the webui type union and Add Server dialog

**Files:**
- Modify: `packages/webui/src/lib/types.ts`
- Modify: `packages/webui/src/components/AddServerDialog.tsx`

**Interfaces:**
- Consumes: `ServerType` union (Task 1 backend enum mirrored here — must match `"pypi" | "npm" | "git" | "cmd" | "proxy"` exactly, since this is what gets posted as JSON `type` to `POST /api/servers`, deserialized by FastAPI/Pydantic into the backend `ServerType` enum).
- Produces: no new exports — extends the existing `ServerType` union and `AddServerDialog` component in place.

- [ ] **Step 1: Extend the `ServerType` union**

In `types.ts`, find:

```ts
export type ServerType = "pypi" | "npm" | "git" | "cmd";
```

Change to:

```ts
export type ServerType = "pypi" | "npm" | "git" | "cmd" | "proxy";
```

- [ ] **Step 2: Add `proxy` to the Add Server dropdown and adjust the package field hint**

In `AddServerDialog.tsx`, find:

```tsx
                <SelectContent>
                  <SelectItem value="pypi">PyPI (uvx)</SelectItem>
                  <SelectItem value="npm">npm (npx)</SelectItem>
                  <SelectItem value="git">Git repo</SelectItem>
                  <SelectItem value="cmd">Command</SelectItem>
                </SelectContent>
```

Change to:

```tsx
                <SelectContent>
                  <SelectItem value="pypi">PyPI (uvx)</SelectItem>
                  <SelectItem value="npm">npm (npx)</SelectItem>
                  <SelectItem value="git">Git repo</SelectItem>
                  <SelectItem value="cmd">Command</SelectItem>
                  <SelectItem value="proxy">Proxy (remote URL)</SelectItem>
                </SelectContent>
```

And find:

```tsx
              placeholder="mcp-server-fetch or git+https://... or /usr/bin/cmd"
```

Change to:

```tsx
              placeholder="mcp-server-fetch or git+https://... or /usr/bin/cmd or http://host:port/mcp"
```

- [ ] **Step 3: Manual verification**

```bash
cd packages/webui
pnpm dev
```

Open the app in a browser, sign in, click "Add server", open the Type dropdown. Expected: five options are present including "Proxy (remote URL)"; selecting it and typing a URL into the "Package / source" field shows the URL as typed (no client-side validation blocks it — the field is a plain required text input). Don't submit unless a real proxy target is running (Task 3 covers the real round-trip); closing the dialog without submitting is sufficient for this step.

- [ ] **Step 4: Commit**

```bash
git add packages/webui/src/lib/types.ts packages/webui/src/components/AddServerDialog.tsx
git commit -m "feat(webui): add proxy option to Add Server dialog"
```

---

### Task 3: End-to-end verification against a real Streamable HTTP target

**Files:** none in the repo — this task creates a throwaway verification server script outside the tracked tree (e.g. in the project's scratch/tmp directory) and runs manual `curl` checks. Per the Global Constraints, this project has no committed test suite; this task is the manual equivalent of one, run once to confirm Tasks 1-2 work together, not committed.

**Interfaces:**
- Consumes: `POST /api/servers`, `GET /api/tools`, `POST /api/tools/call`, `POST /api/servers/{id}/restart`, `DELETE /api/servers/{server_id}` (existing, unchanged, from `packages/aggregator/src/aggregator/api/routers.py`); the `ADMIN_TOKEN` bearer token from `.env` for authenticating these calls (see README's "Local Testing" section).
- Produces: nothing new — this is a verification-only task with no deliverable code.

- [ ] **Step 1: Write a minimal Streamable HTTP target server**

Save as `/tmp/verify_proxy_target.py` (adjust to this session's scratchpad path if preferred — it is not committed):

```python
# /// script
# dependencies = ["mcp"]
# ///
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("verify-echo")


@mcp.tool()
def echo(text: str) -> str:
    """Echo back the given text, uppercased."""
    return text.upper()


if __name__ == "__main__":
    mcp.run(transport="streamable-http", port=8765)
```

(`mcp.server.fastmcp.FastMCP` — the pre-2.0 name for this helper — no longer exists under the installed `mcp==2.0.0`; `mcp.server.mcpserver.MCPServer` is its replacement, confirmed by running this exact script during this plan's revision.)

- [ ] **Step 2: Run the target server in the background**

```bash
uv run /tmp/verify_proxy_target.py &
sleep 1
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/mcp
```

Expected: `400` — the SDK's Streamable HTTP endpoint rejects a bare `curl` GET without a proper MCP session header; this just confirms something is listening on 8765.

- [ ] **Step 3: Start the aggregator and register the proxy server**

With the aggregator running (`just up` or `uv run uvicorn aggregator.main:app --reload` per the README), and `TOKEN` set from `.env`'s `ADMIN_TOKEN`:

```bash
TOKEN=$(grep ^ADMIN_TOKEN .env | cut -d= -f2)

curl -s -X POST http://localhost:8000/api/servers \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"verify-proxy","type":"proxy","package":"http://host.docker.internal:8765/mcp","args":[],"env":{}}' \
  | python3 -m json.tool
```

(If the aggregator itself is running in Docker, `host.docker.internal` reaches the host-run target server from Step 2; if the aggregator is also running locally/non-Docker, use `http://127.0.0.1:8765/mcp` instead.)

Expected: JSON response with `"server": {"type": "proxy", ...}`, `"tools": ["echo"]`, and no `"error"` key.

- [ ] **Step 4: List and call the proxied tool through the aggregator**

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/tools | python3 -m json.tool

curl -s -X POST http://localhost:8000/api/tools/call \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"server":"verify-proxy","tool":"echo","arguments":{"text":"hello"}}' \
  | python3 -m json.tool
```

Expected: the tools list includes `{"server": "verify-proxy", "tool": "echo", ...}`; the call response has `"isError": false` and content containing `"HELLO"`.

- [ ] **Step 5: Restart and delete round-trip**

```bash
SERVER_ID=$(curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/servers | python3 -c "import json,sys; print([s['id'] for s in json.load(sys.stdin) if s['name']=='verify-proxy'][0])")

curl -s -X POST "http://localhost:8000/api/servers/$SERVER_ID/restart" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
curl -s -X DELETE "http://localhost:8000/api/servers/$SERVER_ID" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Expected: restart returns `{"id": ..., "tool_count": 1}`; delete returns `{"deleted": ...}`. Then confirm cleanup:

```bash
kill %1  # stop the background verify_proxy_target.py from Step 2
rm /tmp/verify_proxy_target.py
```

- [ ] **Step 6: Record the result**

No commit for this task (nothing tracked changed). If any expected result in Steps 3-5 didn't match, that's a bug in Task 1 or Task 2 — go back and fix it there (with its own commit) rather than working around it here.
