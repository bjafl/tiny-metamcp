# Monorepo Restructuring + React Admin Webui Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the in-progress uv/pnpm monorepo split (`packages/aggregator` for the Python backend, `packages/webui` for a new frontend) and replace the server-rendered Jinja2/htmx admin panel with a React/TypeScript/Vite SPA that talks to the aggregator's existing `/api/*` JSON surface.

**Architecture:** The Python backend (`packages/aggregator`) nests its source under `src/aggregator/*.py` (restoring the shape the pre-refactor `aggregator/src/mcp_aggregator/` layout had, just relocated and renamed) and builds with uv's own `uv_build` backend — verified to require this nesting (it has no rename support; hatchling's `sources` rename was also tried and fails under uv's editable-install mode — see Task 1). The React SPA (`packages/webui`) is built with Vite and, in production, its static output is served directly by FastAPI under `/admin` — one container, no CORS, cookie auth keeps working same-origin. Auth state is exposed to the SPA via a new `GET /api/me` endpoint; the existing GitHub OAuth cookie flow is otherwise untouched.

**Tech Stack:** Python 3.14, FastAPI, uv workspaces, `uv_build` (aggregator build backend) · React 19, TypeScript, Vite, Tailwind CSS v4, shadcn/ui, TanStack Router, TanStack Query, pnpm workspaces.

**Spec:** `docs/superpowers/specs/2026-08-02-monorepo-webui-design.md`

## Global Constraints

- Root `pyproject.toml` (uv workspace, `members = ["packages/*"]`) and root `package.json` (`packageManager: pnpm@10.33.0`) already exist — do not recreate them, only edit as instructed.
- No changes to MCP protocol endpoints (`/mcp`, `/messages`), OAuth 2.1 client flow (`api/oauth_router.py`), child process management, database schema, or installer logic.
- No new docker-compose services or Traefik routing changes — single `mcp-aggregator` service.
- No test suite exists today and none is being added; each task's "test" step is a concrete manual/CLI verification instead of an automated test.
- Node 22 and pnpm 10.33.0 are the target versions (already pinned in root `package.json`); Python is 3.14 (already pinned in `.python-version`).

## Known pre-existing issue (out of scope, do not fix)

While verifying Task 1's approach, `import aggregator.main` was traced far enough to hit `AttributeError: 'Server' object has no attribute 'list_tools'` in `packages/aggregator/src/aggregator/aggregator.py` (the module named `aggregator.py`, inside the `aggregator` package — same naming this file had before the move, just relocated), caused by the installed `mcp` package (pinned `mcp>=2`, resolves to `mcp==2.0.0`) having a different `Server` API than this code expects. This is unrelated to packaging/monorepo structure and is explicitly out of scope (see Global Constraints). It means the container will still fail to boot after this plan — flag this to the user as a separate follow-up; do not attempt to fix `aggregator.py` as part of these tasks.

---

### Task 1: Fix the uv workspace build (nest the package, fix root + aggregator `pyproject.toml`)

**Files:**
- Move: `packages/aggregator/src/*.py`, `packages/aggregator/src/api/`, `packages/aggregator/src/templates/` → `packages/aggregator/src/aggregator/`
- Modify: `pyproject.toml` (repo root)
- Modify: `packages/aggregator/pyproject.toml`

**Interfaces:**
- Produces: a working `uv sync` at the repo root, and an installed package importable as `import aggregator` (e.g. `aggregator.main`, `aggregator.config`, `aggregator.api.routers`).

Three real, verified issues are being fixed here (confirmed by running `uv sync` against scratch copies of this exact tree):

1. Root `pyproject.toml` declares `dependencies = ["aggregator"]` / `tool.uv.sources.aggregator = { workspace = true }`, but `packages/aggregator/pyproject.toml`'s `[project] name` is `mcp-aggregator` — uv matches workspace members by declared project name, not by directory or install name, so this mismatch makes `uv sync` fail with `"aggregator" references a workspace ... but is not a workspace member`.
2. `packages/aggregator/pyproject.toml` pins `fastapi>=0.142`, but the latest version actually published on PyPI (verified live) is `0.141.1` — this constraint is unresolvable and blocks `uv sync` entirely, independent of anything else in this plan.
3. `packages/aggregator/pyproject.toml` has no `[build-system]` at all, so `uv sync`/`uv build` cannot build it. Three approaches were tested directly against this codebase before settling on the one below:
   - hatchling's `sources` path-rename (`"src" = "aggregator"` on a flat `src/*.py` layout) **fails** under uv's editable-install mode: `ValueError: Dev mode installations are unsupported when any path rewrite in the sources option changes a prefix rather than removes it`.
   - `uv_build` on a flat `src/*.py` layout **fails**: `Expected a Python module at: packages/aggregator/src/aggregator/__init__.py` — `uv_build` has no rename support; `module-root`/`module-name` must match the physical layout exactly.
   - `uv_build` with `src/*.py` nested one level down into `src/aggregator/*.py` **works** — verified end-to-end (`uv sync` + `import aggregator.config/.models/.database/.api`). This is the approach used below. It requires physically moving the files, but zero import statements need to change (every internal import in this codebase is relative — `from . import admin_auth`, `from .config import ...` — which resolves identically regardless of nesting depth).

- [ ] **Step 1: Nest the package source under `src/aggregator/`**

```bash
cd packages/aggregator/src
mkdir aggregator
git mv __init__.py admin_auth.py aggregator.py api child_manager.py config.py database.py installer.py log_capture.py main.py models.py oauth.py templates aggregator/
cd ../../..
```
(`templates/` is moved along with everything else here even though Task 4 deletes it shortly — keeping this step's `git mv` list exhaustive avoids leaving stray files at the old flat location.)

- [ ] **Step 2: Fix `packages/aggregator/pyproject.toml`**

Replace its full contents with:

```toml
[project]
name = "mcp-aggregator"
version = "2.0.1"
requires-python = ">=3.14"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.52",
    "mcp>=2",
    "aiosqlite>=0.20",
    "httpx>=0.27",
    "anyio>=4.11",
    "sse-starlette>=2.1",
    "python-multipart>=0.0.9",
    "itsdangerous>=2.2.0",
    "sqlmodel>=0.0.38",
]

[build-system]
requires = ["uv_build>=0.11,<0.12"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "aggregator"
```

Note `jinja2>=3.1.6` has been dropped from dependencies — Task 4 removes the last code that uses it (Jinja2 templates). If Task 4 has not landed yet when you run this step, leave `"jinja2>=3.1.6",` in the list for now and remove it when you do Task 4 instead.

`module-root` is left at its default (`src`) since it already matches; only `module-name` needs to be set because it would otherwise default to the normalized project name (`mcp_aggregator`, not `aggregator`).

- [ ] **Step 3: Fix root `pyproject.toml`**

Change:
```toml
dependencies = ["aggregator"]

[tool.uv.sources]
aggregator = { workspace = true }
```
to:
```toml
dependencies = ["mcp-aggregator"]

[tool.uv.sources]
mcp-aggregator = { workspace = true }
```
Leave `[tool.uv.workspace] members = ["packages/*"]` and everything else in the file unchanged.

- [ ] **Step 4: Verify the build**

Run from repo root:
```bash
uv sync
uv run python -c "
import aggregator
print('aggregator OK:', aggregator.__file__)
import aggregator.config
import aggregator.models
import aggregator.database
import aggregator.api
print('all submodules import OK')
"
```
Expected: `uv sync` completes with no errors, and the script prints `aggregator OK: .../packages/aggregator/src/aggregator/__init__.py` followed by `all submodules import OK`. Do **not** run `import aggregator.main` or `import aggregator.aggregator` as a verification step — those hit the pre-existing `mcp` API bug documented above and are expected to fail; that failure is not something this task should chase.

- [ ] **Step 5: Commit**

```bash
git add packages/aggregator/src pyproject.toml packages/aggregator/pyproject.toml
git commit -m "fix(aggregator): nest package under src/aggregator/, fix uv workspace build (name mismatch, fastapi pin, uv_build backend)"
```

---

### Task 2: Fix docker-compose build context

**Files:**
- Modify: `docker-compose.yml`

**Interfaces:**
- Produces: a `mcp-aggregator` service definition whose build context matches where Task 11's multi-stage Dockerfile expects to find both `packages/aggregator` and `packages/webui`.

The Dockerfile built in Task 11 needs to `COPY` from both `packages/aggregator/` and `packages/webui/` (plus root `pnpm-workspace.yaml`/`package.json`) in the same build — that requires the build context to be the repo root, with the Dockerfile path pointed at `packages/aggregator/Dockerfile` explicitly (a context scoped to `./packages/aggregator` alone cannot see `packages/webui`, since Docker build contexts cannot reach outside their root).

- [ ] **Step 1: Edit `docker-compose.yml`**

Change:
```yaml
  mcp-aggregator:
    build:
      context: ./aggregator
      dockerfile: Dockerfile
```
to:
```yaml
  mcp-aggregator:
    build:
      context: .
      dockerfile: packages/aggregator/Dockerfile
```
Leave everything else in the file (`container_name`, `environment`, `volumes`, `healthcheck`, `labels`) unchanged.

- [ ] **Step 2: Verify**

```bash
grep -A3 "build:" docker-compose.yml
```
Expected output shows `context: .` and `dockerfile: packages/aggregator/Dockerfile`. (Do not `docker compose build` yet — the Dockerfile itself isn't updated until Task 11, so a build now would still fail on the old `./aggregator` paths inside it.)

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "fix(compose): point aggregator build context at repo root for the new package layout"
```

---

### Task 3: Add `GET /api/me` session-check endpoint

**Files:**
- Modify: `packages/aggregator/src/aggregator/main.py`

**Interfaces:**
- Consumes: `admin_auth.get_session_user(request) -> str | None` (existing, `packages/aggregator/src/aggregator/admin_auth.py:34`).
- Produces: `GET /api/me` → `200 {"username": str}` on a valid session cookie, `401 {"detail": "Not authenticated"}` otherwise. This is what the webui's `useMe()` hook (Task 6/7) polls to decide whether to show the app or redirect to `/admin/login`.

This is deliberately **not** added to `api/routers.py`'s `router` (which requires `require_api_auth` — session cookie *or* static `ADMIN_TOKEN` bearer token). `/api/me` is specifically about GitHub session identity for the SPA; a bearer-token caller has no "username" to report, so it lives as its own route using `get_session_user` directly.

- [ ] **Step 1: Add the route**

In `packages/aggregator/src/aggregator/main.py`, add this immediately after the existing `admin_logout` route (i.e. right after the block ending `return response` for `GET /admin/logout`, before the `# ── Admin UI (session required) ──` comment):

```python
@app.get("/api/me")
async def api_me(request: Request):
    user = admin_auth.get_session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": user}
```

- [ ] **Step 2: Verify manually**

Start the server locally (`uv run uvicorn aggregator.main:app --reload` from `packages/aggregator`, once Task 1 is done) and check:
```bash
curl -i http://localhost:8000/api/me
```
Expected: `HTTP/1.1 401 Unauthorized` with body `{"detail":"Not authenticated"}`. (A `200` with a username requires an actual signed session cookie, which needs the GitHub OAuth flow — not practical to script here; the 401 case alone confirms the route and the "no session" branch work.)

- [ ] **Step 3: Commit**

```bash
git add packages/aggregator/src/aggregator/main.py
git commit -m "feat(aggregator): add GET /api/me session-check endpoint for the SPA"
```

---

### Task 4: Remove the Jinja2/htmx admin panel

**Files:**
- Modify: `packages/aggregator/src/aggregator/main.py`
- Delete: `packages/aggregator/src/aggregator/templates/` (entire directory: `admin/index.html`, `admin/_servers_table.html`, `admin/_add_result.html`, `admin/login.html`, `base.html`)
- Modify: `packages/aggregator/pyproject.toml` (drop `jinja2` dependency if you left it in during Task 1)

**Interfaces:**
- Consumes: none new.
- Produces: `main.py` with only the routes that stay backend-owned: `/mcp`, `/messages`, `/health`, `/admin/login/github`, `/admin/logout`, `/api/me` (Task 3), plus everything mounted via `api_router`/`oauth_router`. All server-rendered admin HTML is gone — Task 11 adds the SPA-serving replacement for `/admin` and `/admin/login`.

Every route being deleted here (`GET /admin`, `GET /admin/login`, `GET /admin/servers-table`, `POST /admin/add-server`, `POST /admin/servers/{id}/enable`, `POST /admin/servers/{id}/disable`, `POST /admin/servers/{id}/restart`, `DELETE /admin/servers/{id}`) has an equivalent already live under `/api/*` (`api/routers.py`) that the new SPA will call instead — confirmed by reading both files side by side. `GET /admin/login/github` and `GET /admin/logout` are **kept** — they're real redirect-driving backend behavior (start the GitHub OAuth flow, clear the session cookie), not HTML rendering.

- [ ] **Step 1: Delete the templates directory**

```bash
rm -rf packages/aggregator/src/aggregator/templates
```

- [ ] **Step 2: Edit `packages/aggregator/src/aggregator/main.py` — trim imports**

Change:
```python
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import admin_auth, log_capture, oauth
from .aggregator import mcp_server, sse_transport
from .api.oauth_router import router as oauth_router
from .api.routers import router as api_router
from .child_manager import child_manager
from .config import ADMIN_TOKEN, LOG_LEVEL
from .database import (
    add_server,
    delete_server,
    get_server,
    init_db,
    list_servers,
    update_server_enabled,
)
from .installer import uninstall
from .models import ServerType
```
to:
```python
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import admin_auth, log_capture, oauth
from .aggregator import mcp_server, sse_transport
from .api.oauth_router import router as oauth_router
from .api.routers import router as api_router
from .child_manager import child_manager
from .config import ADMIN_TOKEN, LOG_LEVEL
from .database import init_db, list_servers
```
(`add_server`, `delete_server`, `get_server`, `update_server_enabled`, `uninstall`, `ServerType`, `Path`, `HTMLResponse`, `Jinja2Templates` were only used by the code being deleted below — `api/routers.py` already imports its own copies for the `/api/*` endpoints, untouched.)

- [ ] **Step 3: Remove the `Jinja2Templates` instance**

Delete this line (currently right after the imports):
```python
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
```

- [ ] **Step 4: Delete the `GET /admin/login` route**

Delete:
```python
@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "admin/login.html", {"error": error})
```
Keep the `GET /admin/login/github` and `GET /admin/logout` routes immediately below it exactly as they are.

- [ ] **Step 5: Delete the HTML-fragment admin routes**

Delete the entire block starting at `@app.get("/admin", response_class=HTMLResponse)` (the `admin_root` route) through the end of the `admin_delete` function (`DELETE /admin/servers/{server_id}`) — this includes `admin_root`, the `_render_servers_table` helper, `admin_servers_table`, `admin_add_server`, `admin_enable`, `admin_disable`, `admin_restart`, and `admin_delete`. Nothing should remain in the file after the `GET /api/me` route added in Task 3 except whatever Task 11 adds later.

- [ ] **Step 6: Drop `jinja2` from `packages/aggregator/pyproject.toml` if still present**

If you kept `"jinja2>=3.1.6",` in the `dependencies` list during Task 1, remove that line now — nothing imports `jinja2` anymore.

- [ ] **Step 7: Verify**

```bash
grep -n "Jinja2\|templates\." packages/aggregator/src/aggregator/main.py
```
Expected: no output (no matches).
```bash
uv run python -c "import aggregator.config, aggregator.models, aggregator.database, aggregator.api" 
```
Expected: no errors (same scoped-import check as Task 1, since `main.py` itself still hits the pre-existing out-of-scope `mcp` bug on full import).
```bash
python3 -c "import ast; ast.parse(open('packages/aggregator/src/aggregator/main.py').read())"
```
Expected: no output (confirms `main.py` is still syntactically valid Python after the manual edits).

- [ ] **Step 8: Commit**

```bash
git add packages/aggregator/src/aggregator/main.py packages/aggregator/pyproject.toml
git rm -r packages/aggregator/src/aggregator/templates
git commit -m "refactor(aggregator): remove Jinja2/htmx admin panel, superseded by /api/* + new webui"
```

---

### Task 5: Scaffold `packages/webui` (Vite + React + TS + Tailwind + shadcn/ui + TanStack Router/Query)

**Files:**
- Create: `packages/webui/` (via `pnpm create vite`)
- Create: `pnpm-workspace.yaml` (repo root)
- Modify: `package.json` (repo root)
- Modify: `.gitignore` (repo root)

**Interfaces:**
- Produces: a `packages/webui` app that builds (`pnpm --filter webui build`) and runs in dev (`pnpm --filter webui dev`) with Tailwind v4, shadcn/ui primitives, TanStack Router, and TanStack Query installed and wired, plus a dev proxy so `/api`, `/admin/login*`, `/oauth/*` requests reach the FastAPI backend on `localhost:8000`.

This exact scaffold sequence (Vite 8 / React 19 / TS ~6 template, then Tailwind v4 via `@tailwindcss/vite`, then shadcn's official Vite recipe) was run and verified to produce a working project during planning.

- [ ] **Step 1: Scaffold the Vite app**

From the repo root:
```bash
pnpm create vite@latest packages/webui --template react-ts
cd packages/webui
```

- [ ] **Step 2: Add Tailwind CSS v4**

```bash
pnpm add tailwindcss @tailwindcss/vite
```

Replace the full contents of `src/index.css` with:
```css
@import "tailwindcss";
```

- [ ] **Step 3: Add path alias — `tsconfig.json`**

The scaffolded `tsconfig.json` is:
```json
{
  "files": [],
  "references": [
    { "path": "./tsconfig.app.json" },
    { "path": "./tsconfig.node.json" }
  ]
}
```
Change it to:
```json
{
  "files": [],
  "references": [
    { "path": "./tsconfig.app.json" },
    { "path": "./tsconfig.node.json" }
  ],
  "compilerOptions": {
    "baseUrl": ".",
    "paths": {
      "@/*": ["./src/*"]
    }
  }
}
```

- [ ] **Step 4: Add path alias — `tsconfig.app.json`**

Add `"baseUrl": "."` and `"paths": {"@/*": ["./src/*"]}` inside the existing `compilerOptions` object (keep every other key as scaffolded):
```json
{
  "compilerOptions": {
    "tsBuildInfoFile": "./node_modules/.tmp/tsconfig.app.tsbuildinfo",
    "target": "es2023",
    "lib": ["ES2023", "DOM"],
    "module": "esnext",
    "types": ["vite/client"],
    "allowArbitraryExtensions": true,
    "skipLibCheck": true,
    "baseUrl": ".",
    "paths": {
      "@/*": ["./src/*"]
    },

    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "verbatimModuleSyntax": true,
    "moduleDetection": "force",
    "noEmit": true,
    "jsx": "react-jsx",

    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "erasableSyntaxOnly": true,
    "noFallthroughCasesInSwitch": true
  },
  "include": ["src"]
}
```

- [ ] **Step 5: Rewrite `vite.config.ts`**

`@types/node` is already a scaffolded devDependency, so `node:path` resolves without extra installs. Replace the full contents of `vite.config.ts` with:
```ts
import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  base: "/admin/",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/admin/login": "http://localhost:8000",
      "/admin/logout": "http://localhost:8000",
      "/oauth": "http://localhost:8000",
    },
  },
});
```

- [ ] **Step 6: Run shadcn init**

```bash
pnpm dlx shadcn@latest init
```
When prompted, accept the defaults (base color: neutral or slate is fine — no functional impact on the components used in this plan). This generates `components.json`, `src/lib/utils.ts` (the `cn()` helper), and rewrites `src/index.css` to add the shadcn CSS variable theme on top of the `@import "tailwindcss";` line added in Step 2 — that's expected and correct.

- [ ] **Step 7: Add the shadcn primitives this plan uses**

```bash
pnpm dlx shadcn@latest add button badge table dialog input textarea select label
```
This creates `src/components/ui/{button,badge,table,dialog,input,textarea,select,label}.tsx`. Later tasks import from these paths — do not rename them.

- [ ] **Step 8: Remove Vite template boilerplate**

Delete the scaffolded demo content, since it'll be replaced by the real app in Task 7:
```bash
rm -f src/App.css src/assets/react.svg
```
Leave `src/App.tsx` and `src/main.tsx` in place for now — Task 7 rewrites both.

- [ ] **Step 9: Wire the monorepo root**

Create `pnpm-workspace.yaml` at the repo root:
```yaml
packages:
  - "packages/*"
```

Edit root `package.json` — add a `build` and `dev:webui` script (keep every existing key, including the `test` placeholder and `packageManager`):
```json
{
  "name": "custom-mcp-meta",
  "version": "1.0.0",
  "description": "",
  "main": "index.js",
  "scripts": {
    "test": "echo \"Error: no test specified\" && exit 1",
    "build": "pnpm --filter webui build && rm -rf packages/aggregator/webui_dist && cp -r packages/webui/dist packages/aggregator/webui_dist",
    "dev:webui": "pnpm --filter webui dev"
  },
  "keywords": [],
  "author": "",
  "license": "ISC",
  "packageManager": "pnpm@10.33.0"
}
```

Add to root `.gitignore` (it currently has no Node-related entries at all):
```
node_modules/
```

- [ ] **Step 10: Install and verify**

From the repo root:
```bash
pnpm install
pnpm --filter webui build
```
Expected: install completes with no errors, and `pnpm --filter webui build` produces `packages/webui/dist/index.html` plus a `packages/webui/dist/assets/` directory with no TypeScript or build errors (the template's default `App.tsx` still compiles fine even with the alias/Tailwind changes — that's what you're checking at this step, not the final app).

- [ ] **Step 11: Commit**

```bash
git add packages/webui pnpm-workspace.yaml package.json .gitignore
git commit -m "feat(webui): scaffold Vite/React/TS app with Tailwind v4, shadcn/ui, pnpm workspace wiring"
```

---

### Task 6: API client, types, and TanStack Query hooks

**Files:**
- Create: `packages/webui/src/lib/types.ts`
- Create: `packages/webui/src/lib/api.ts`
- Create: `packages/webui/src/hooks/useMe.ts`
- Create: `packages/webui/src/hooks/useServers.ts`
- Create: `packages/webui/src/hooks/useTools.ts`

**Interfaces:**
- Produces: `ServerConfig`, `AddServerInput`, `AddServerResult`, `ToolInfo`, `CallToolInput`, `CallToolResult`, `LogEntry`, `Me` types; `api.me/listServers/addServer/deleteServer/enableServer/disableServer/restartServer/listTools/callTool` functions; `class ApiError extends Error { status: number }`; `meQueryOptions`, `useMe()`, `useServers()`, `useAddServer()`, `useDeleteServer()`, `useEnableServer()`, `useDisableServer()`, `useRestartServer()`, `useTools()`, `useCallTool()` hooks. Tasks 7-10 consume these exclusively — no direct `fetch()` calls anywhere else in the app.
- Consumes: the backend's actual JSON shapes, read directly from `packages/aggregator/src/aggregator/api/routers.py` and `packages/aggregator/src/aggregator/models.py` (`ServerType` enum values `pypi`/`npm`/`git`/`cmd`; `Server._cfg()`'s dict shape; `log_capture.LogEntry.as_dict()`'s `{ts, level, server, msg}` shape).

- [ ] **Step 1: Install TanStack Router and Query**

```bash
cd packages/webui
pnpm add @tanstack/react-router @tanstack/react-query
```

- [ ] **Step 2: `src/lib/types.ts`**

```ts
export type ServerType = "pypi" | "npm" | "git" | "cmd";

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
}

export interface AddServerInput {
  name: string;
  type: ServerType;
  package: string;
  args: string[];
  env: Record<string, string>;
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
}
```

- [ ] **Step 3: `src/lib/api.ts`**

```ts
import type {
  AddServerInput,
  AddServerResult,
  CallToolInput,
  CallToolResult,
  Me,
  ServerConfig,
  ToolInfo,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    credentials: "same-origin",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      // response body wasn't JSON — keep statusText
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  me: () => request<Me>("/api/me"),
  listServers: () => request<ServerConfig[]>("/api/servers"),
  addServer: (input: AddServerInput) =>
    request<AddServerResult>("/api/servers", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  deleteServer: (id: number) =>
    request<{ deleted: number }>(`/api/servers/${id}`, { method: "DELETE" }),
  enableServer: (id: number) =>
    request<{ id: number; enabled: true; tool_count: number }>(
      `/api/servers/${id}/enable`,
      { method: "POST" },
    ),
  disableServer: (id: number) =>
    request<{ id: number; enabled: false }>(`/api/servers/${id}/disable`, {
      method: "POST",
    }),
  restartServer: (id: number) =>
    request<{ id: number; tool_count: number }>(`/api/servers/${id}/restart`, {
      method: "POST",
    }),
  listTools: () => request<ToolInfo[]>("/api/tools"),
  callTool: (input: CallToolInput) =>
    request<CallToolResult>("/api/tools/call", {
      method: "POST",
      body: JSON.stringify(input),
    }),
};
```

- [ ] **Step 4: `src/hooks/useMe.ts`**

```ts
import { queryOptions, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export const meQueryOptions = queryOptions({
  queryKey: ["me"],
  queryFn: api.me,
  retry: false,
});

export function useMe() {
  return useQuery(meQueryOptions);
}
```

- [ ] **Step 5: `src/hooks/useServers.ts`**

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { AddServerInput } from "@/lib/types";

const serversKey = ["servers"] as const;

export function useServers() {
  return useQuery({
    queryKey: serversKey,
    queryFn: api.listServers,
    refetchInterval: 5000,
  });
}

export function useAddServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: AddServerInput) => api.addServer(input),
    onSuccess: () => qc.invalidateQueries({ queryKey: serversKey }),
  });
}

export function useDeleteServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.deleteServer(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: serversKey }),
  });
}

export function useEnableServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.enableServer(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: serversKey }),
  });
}

export function useDisableServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.disableServer(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: serversKey }),
  });
}

export function useRestartServer() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.restartServer(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: serversKey }),
  });
}
```

- [ ] **Step 6: `src/hooks/useTools.ts`**

```ts
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { CallToolInput } from "@/lib/types";

export function useTools() {
  return useQuery({ queryKey: ["tools"], queryFn: api.listTools });
}

export function useCallTool() {
  return useMutation({
    mutationFn: (input: CallToolInput) => api.callTool(input),
  });
}
```

- [ ] **Step 7: Verify**

```bash
cd packages/webui
pnpm exec tsc -b --noEmit
```
Expected: no output (all new files type-check cleanly against `tsconfig.app.json`'s strict settings, including `verbatimModuleSyntax`).

- [ ] **Step 8: Commit**

```bash
git add packages/webui/src/lib packages/webui/src/hooks packages/webui/package.json packages/webui/pnpm-lock.yaml 2>/dev/null
git add packages/webui pnpm-lock.yaml
git commit -m "feat(webui): API client, types, and TanStack Query hooks for servers/tools/me"
```

---

### Task 7: App shell — router, auth guard, layout, login page

**Files:**
- Create: `packages/webui/src/router.tsx`
- Create: `packages/webui/src/components/AppLayout.tsx`
- Create: `packages/webui/src/components/LoginPage.tsx`
- Modify: `packages/webui/src/main.tsx`
- Delete: `packages/webui/src/App.tsx` (no longer used — routing replaces it)

**Interfaces:**
- Consumes: `meQueryOptions` (Task 6, `src/hooks/useMe.ts`), `useMe()` (Task 6), shadcn `Button` (`@/components/ui/button`, Task 5).
- Produces: `rootRoute`, `loginRoute`, `serversRoute`, `logsRoute`, `testerRoute` route objects and `createAppRouter(queryClient)` from `src/router.tsx` — Task 8/9/10 each fill in one route's `component`. Until those land, `serversRoute`/`logsRoute`/`testerRoute` use trivial placeholder components defined inline in this task so the app is runnable end-to-end after this task; Tasks 8-10 replace those placeholders with the real page components (`ServersPage`, `LogsPage`, `ToolTesterPage`) and re-point each route's `component`.

- [ ] **Step 1: `src/router.tsx`**

```tsx
import {
  createRootRouteWithContext,
  createRoute,
  createRouter,
  Outlet,
  redirect,
} from "@tanstack/react-router";
import type { QueryClient } from "@tanstack/react-query";
import { meQueryOptions } from "@/hooks/useMe";
import { AppLayout } from "@/components/AppLayout";
import { LoginPage } from "@/components/LoginPage";

interface RouterContext {
  queryClient: QueryClient;
}

export const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: () => <Outlet />,
});

export const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  validateSearch: (search: Record<string, unknown>) => ({
    error: typeof search.error === "string" ? search.error : undefined,
  }),
  component: LoginPage,
});

const authedLayoutRoute = createRoute({
  id: "_authed",
  getParentRoute: () => rootRoute,
  beforeLoad: async ({ context }) => {
    try {
      await context.queryClient.ensureQueryData(meQueryOptions);
    } catch {
      throw redirect({ to: "/login" });
    }
  },
  component: AppLayout,
});

export const serversRoute = createRoute({
  getParentRoute: () => authedLayoutRoute,
  path: "/",
  component: () => <p>Servers page placeholder — replaced in Task 8</p>,
});

export const logsRoute = createRoute({
  getParentRoute: () => authedLayoutRoute,
  path: "/logs",
  component: () => <p>Logs page placeholder — replaced in Task 9</p>,
});

export const testerRoute = createRoute({
  getParentRoute: () => authedLayoutRoute,
  path: "/tester",
  component: () => <p>Tool tester placeholder — replaced in Task 10</p>,
});

const routeTree = rootRoute.addChildren([
  loginRoute,
  authedLayoutRoute.addChildren([serversRoute, logsRoute, testerRoute]),
]);

export function createAppRouter(queryClient: QueryClient) {
  return createRouter({
    routeTree,
    basepath: "/admin",
    context: { queryClient },
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
```

- [ ] **Step 2: `src/components/AppLayout.tsx`**

```tsx
import { Link, Outlet } from "@tanstack/react-router";
import { useMe } from "@/hooks/useMe";
import { Button } from "@/components/ui/button";

export function AppLayout() {
  const { data: me } = useMe();

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b">
        <nav className="mx-auto flex max-w-5xl items-center justify-between px-4 py-3">
          <div className="flex items-center gap-6">
            <span className="font-semibold">MCP Aggregator</span>
            <Link
              to="/"
              activeOptions={{ exact: true }}
              activeProps={{ className: "font-semibold text-foreground" }}
              className="text-sm text-muted-foreground"
            >
              Servers
            </Link>
            <Link
              to="/logs"
              activeProps={{ className: "font-semibold text-foreground" }}
              className="text-sm text-muted-foreground"
            >
              Logs
            </Link>
            <Link
              to="/tester"
              activeProps={{ className: "font-semibold text-foreground" }}
              className="text-sm text-muted-foreground"
            >
              Tool Tester
            </Link>
          </div>
          <div className="flex items-center gap-3 text-sm text-muted-foreground">
            <span>{me?.username}</span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                window.location.href = "/admin/logout";
              }}
            >
              Logout
            </Button>
          </div>
        </nav>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
```

- [ ] **Step 3: `src/components/LoginPage.tsx`**

```tsx
import { loginRoute } from "@/router";

export function LoginPage() {
  const { error } = loginRoute.useSearch();
  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="w-full max-w-sm space-y-4 text-center">
        <h1 className="text-2xl font-semibold">MCP Aggregator</h1>
        <p className="text-muted-foreground">
          Sign in with GitHub to access the admin interface.
        </p>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
        <a
          href="/admin/login/github"
          className="inline-block rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
        >
          Login with GitHub
        </a>
      </div>
    </div>
  );
}
```

Note this reads `error` from `loginRoute.useSearch()` — the query param the backend's `admin_auth._login_error()` already appends on OAuth failure (`/admin/login?error=...`), unchanged by this plan.

- [ ] **Step 4: Rewrite `src/main.tsx`**

```tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { createAppRouter } from "./router";
import "./index.css";

const queryClient = new QueryClient();
const router = createAppRouter(queryClient);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
```

- [ ] **Step 5: Delete the unused template component**

```bash
rm -f packages/webui/src/App.tsx
```

- [ ] **Step 6: Verify**

```bash
cd packages/webui
pnpm exec tsc -b --noEmit
pnpm dev
```
With the FastAPI backend running separately on `localhost:8000` (Task 1-4 done), open `http://localhost:5173/admin/` in a browser:
- Expected if not logged in: redirected to `http://localhost:5173/admin/login`, showing the "Login with GitHub" button.
- Click it → should proxy through to the real backend's `/admin/login/github` and start the GitHub OAuth flow (this requires `GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET`/`MCP_DOMAIN` to be configured in `.env` for a full round-trip — if those aren't set up in your dev environment, it's enough to confirm the redirect to `github.com/login/oauth/authorize` happens, without completing the login).
Stop the dev server (Ctrl-C) when done.

- [ ] **Step 7: Commit**

```bash
git add packages/webui/src
git commit -m "feat(webui): app shell — TanStack Router, auth guard via /api/me, login page"
```

---

### Task 8: Servers tab

**Files:**
- Create: `packages/webui/src/components/StatusBadge.tsx`
- Create: `packages/webui/src/components/ServerTable.tsx`
- Create: `packages/webui/src/components/AddServerDialog.tsx`
- Create: `packages/webui/src/components/ServersPage.tsx`
- Modify: `packages/webui/src/router.tsx` (point `serversRoute.component` at `ServersPage`)

**Interfaces:**
- Consumes: `useServers`, `useAddServer`, `useEnableServer`, `useDisableServer`, `useRestartServer`, `useDeleteServer` (Task 6); `ServerConfig`, `ServerType` (Task 6, `src/lib/types.ts`); shadcn `Table`/`TableHeader`/`TableBody`/`TableRow`/`TableHead`/`TableCell`, `Badge`, `Button`, `Dialog`/`DialogContent`/`DialogHeader`/`DialogTitle`/`DialogTrigger`/`DialogFooter`, `Input`, `Label`, `Select`/`SelectContent`/`SelectItem`/`SelectTrigger`/`SelectValue` (Task 5).
- Produces: `<ServersPage />`, feature-complete replacement for the old panel's "Servers" tab (list with status, add, enable/disable/restart/delete).

- [ ] **Step 1: `src/components/StatusBadge.tsx`**

```tsx
import { Badge } from "@/components/ui/badge";
import type { ServerConfig } from "@/lib/types";

export function StatusBadge({ server }: { server: ServerConfig }) {
  if (!server.enabled) return <Badge variant="secondary">Disabled</Badge>;
  if (server.error) return <Badge variant="destructive">Error</Badge>;
  if (server.running) {
    return (
      <Badge className="bg-emerald-600 hover:bg-emerald-600">
        Running ({server.tool_count})
      </Badge>
    );
  }
  return <Badge variant="outline">Starting…</Badge>;
}
```

- [ ] **Step 2: `src/components/ServerTable.tsx`**

```tsx
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

  return (
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
  );
}
```

- [ ] **Step 3: `src/components/AddServerDialog.tsx`**

```tsx
import { useState } from "react";
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
import { useAddServer } from "@/hooks/useServers";
import type { ServerType } from "@/lib/types";

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

export function AddServerDialog() {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [type, setType] = useState<ServerType>("pypi");
  const [pkg, setPkg] = useState("");
  const [args, setArgs] = useState("");
  const [env, setEnv] = useState("");
  const addServer = useAddServer();

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    await addServer.mutateAsync({
      name,
      type,
      package: pkg,
      args: parseArgs(args),
      env: parseEnv(env),
    });
    setOpen(false);
    setName("");
    setPkg("");
    setArgs("");
    setEnv("");
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>Add server</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add server</DialogTitle>
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
              placeholder="mcp-server-fetch or git+https://... or /usr/bin/cmd"
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
          {addServer.isError ? (
            <p className="text-sm text-destructive">{addServer.error.message}</p>
          ) : null}
          {addServer.data?.error ? (
            <p className="text-sm text-destructive">
              Started with error: {addServer.data.error}
            </p>
          ) : null}
          <DialogFooter>
            <Button type="submit" disabled={addServer.isPending}>
              {addServer.isPending ? "Installing…" : "Add server"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 4: `src/components/ServersPage.tsx`**

```tsx
import { useServers } from "@/hooks/useServers";
import { ServerTable } from "@/components/ServerTable";
import { AddServerDialog } from "@/components/AddServerDialog";

export function ServersPage() {
  const { data, isLoading, error } = useServers();

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Servers</h1>
        <AddServerDialog />
      </div>
      {isLoading ? <p className="text-muted-foreground">Loading servers…</p> : null}
      {error ? <p className="text-destructive">{error.message}</p> : null}
      {data ? <ServerTable servers={data} /> : null}
    </div>
  );
}
```

- [ ] **Step 5: Point the route at the real page**

In `packages/webui/src/router.tsx`, add `import { ServersPage } from "@/components/ServersPage";` near the other component imports, and change:
```tsx
export const serversRoute = createRoute({
  getParentRoute: () => authedLayoutRoute,
  path: "/",
  component: () => <p>Servers page placeholder — replaced in Task 8</p>,
});
```
to:
```tsx
export const serversRoute = createRoute({
  getParentRoute: () => authedLayoutRoute,
  path: "/",
  component: ServersPage,
});
```

- [ ] **Step 6: Verify**

```bash
cd packages/webui
pnpm exec tsc -b --noEmit
pnpm dev
```
With the backend running and a logged-in session (from Task 7's manual OAuth check, or by manually setting an `admin_session` cookie), open `/admin/` — expected: a table of servers (empty state is fine if none configured), an "Add server" button that opens a dialog, and enable/disable/restart/delete buttons that visibly update the table after a couple seconds (via the 5s poll + mutation-triggered invalidation).

- [ ] **Step 7: Commit**

```bash
git add packages/webui/src
git commit -m "feat(webui): servers tab — list, add, enable/disable/restart/delete"
```

---

### Task 9: Logs tab

**Files:**
- Create: `packages/webui/src/components/LogsPage.tsx`
- Modify: `packages/webui/src/router.tsx` (point `logsRoute.component` at `LogsPage`)

**Interfaces:**
- Consumes: `useServers` (Task 6/8), `LogEntry` type (Task 6), shadcn `Select`/`Button` (Task 5). Talks directly to `/api/logs/stream` via `EventSource` (SSE doesn't fit the query-cache model — same approach the old Alpine.js panel used).
- Produces: `<LogsPage />`, feature-complete replacement for the old panel's "Logs" tab (live stream, server/level filters, connect indicator, auto-scroll).

- [ ] **Step 1: `src/components/LogsPage.tsx`**

```tsx
import { useEffect, useMemo, useRef, useState } from "react";
import { useServers } from "@/hooks/useServers";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import type { LogEntry } from "@/lib/types";

const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;
const LEVEL_COLOR: Record<string, string> = {
  DEBUG: "text-slate-400",
  INFO: "text-emerald-400",
  WARNING: "text-amber-300",
  ERROR: "text-rose-400",
};
const ALL = "__all__";

export function LogsPage() {
  const { data: servers } = useServers();
  const [serverFilter, setServerFilter] = useState<string>(ALL);
  const [levelFilter, setLevelFilter] = useState<string>(ALL);
  const [lines, setLines] = useState<LogEntry[]>([]);
  const [connected, setConnected] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setLines([]);
    const url =
      serverFilter === ALL
        ? "/api/logs/stream"
        : `/api/logs/stream?server=${encodeURIComponent(serverFilter)}`;
    const es = new EventSource(url);
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = (e) => {
      const entry = JSON.parse(e.data) as LogEntry;
      setLines((prev) => (prev.length >= 2000 ? [...prev.slice(1), entry] : [...prev, entry]));
    };
    return () => es.close();
  }, [serverFilter]);

  useEffect(() => {
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [lines]);

  const filtered = useMemo(
    () => lines.filter((l) => levelFilter === ALL || l.level === levelFilter),
    [lines, levelFilter],
  );

  const runningServers = (servers ?? []).filter((s) => s.running).map((s) => s.name);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <Select value={serverFilter} onValueChange={setServerFilter}>
          <SelectTrigger className="w-48">
            <SelectValue placeholder="All servers" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All servers</SelectItem>
            {runningServers.map((name) => (
              <SelectItem key={name} value={name}>
                {name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={levelFilter} onValueChange={setLevelFilter}>
          <SelectTrigger className="w-36">
            <SelectValue placeholder="All levels" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All levels</SelectItem>
            {LEVELS.map((lvl) => (
              <SelectItem key={lvl} value={lvl}>
                {lvl}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button variant="outline" size="sm" onClick={() => setLines([])}>
          Clear
        </Button>
        <span className={connected ? "text-sm text-emerald-500" : "text-sm text-destructive"}>
          {connected ? "● Live" : "○ Disconnected"}
        </span>
      </div>
      <div
        ref={boxRef}
        className="h-96 overflow-y-auto rounded-md bg-slate-950 p-3 font-mono text-xs"
      >
        {filtered.length === 0 ? (
          <span className="text-slate-500">No log entries yet.</span>
        ) : null}
        {filtered.map((l, i) => (
          <div key={`${l.ts}-${i}`} className="flex gap-2">
            <span className="min-w-[8ch] text-slate-500">
              {new Date(l.ts * 1000).toTimeString().slice(0, 8)}
            </span>
            <span className={`min-w-[7ch] font-semibold ${LEVEL_COLOR[l.level] ?? ""}`}>
              {l.level}
            </span>
            <span className="min-w-[10ch] text-sky-400">{l.server || "-"}</span>
            <span className="break-all text-slate-200">{l.msg}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Point the route at the real page**

In `packages/webui/src/router.tsx`, add `import { LogsPage } from "@/components/LogsPage";` and change `logsRoute`'s `component` from the placeholder to `LogsPage`.

- [ ] **Step 3: Verify**

```bash
cd packages/webui
pnpm exec tsc -b --noEmit
pnpm dev
```
Open `/admin/logs` with the backend running and at least one server configured/running — expected: `● Live` indicator, log lines appearing as the aggregator logs events (e.g. trigger one by enabling/disabling a server from the Servers tab), server/level filters narrowing the visible lines, "Clear" emptying the view.

- [ ] **Step 4: Commit**

```bash
git add packages/webui/src
git commit -m "feat(webui): logs tab — live SSE stream with server/level filters"
```

---

### Task 10: Tool Tester tab

**Files:**
- Create: `packages/webui/src/components/ToolTesterPage.tsx`
- Modify: `packages/webui/src/router.tsx` (point `testerRoute.component` at `ToolTesterPage`)

**Interfaces:**
- Consumes: `useTools`, `useCallTool` (Task 6), `ToolInfo` (Task 6), shadcn `Select`/`Button`/`Textarea` (Task 5).
- Produces: `<ToolTesterPage />`, feature-complete replacement for the old panel's "Tool Tester" tab (server/tool pickers, input schema view, JSON args, call + result display).

- [ ] **Step 1: `src/components/ToolTesterPage.tsx`**

```tsx
import { useMemo, useState } from "react";
import { useCallTool, useTools } from "@/hooks/useTools";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

export function ToolTesterPage() {
  const { data: tools } = useTools();
  const callTool = useCallTool();
  const [server, setServer] = useState("");
  const [tool, setTool] = useState("");
  const [args, setArgs] = useState("{}");

  const servers = useMemo(() => [...new Set((tools ?? []).map((t) => t.server))], [tools]);
  const toolsForServer = useMemo(
    () => (tools ?? []).filter((t) => t.server === server),
    [tools, server],
  );
  const schema = toolsForServer.find((t) => t.tool === tool)?.inputSchema;

  function handleCall() {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(args);
    } catch {
      alert("Invalid JSON in arguments");
      return;
    }
    callTool.mutate({ server, tool, arguments: parsed });
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <Select
          value={server}
          onValueChange={(v) => {
            setServer(v);
            setTool("");
          }}
        >
          <SelectTrigger>
            <SelectValue placeholder="Select server" />
          </SelectTrigger>
          <SelectContent>
            {servers.map((s) => (
              <SelectItem key={s} value={s}>
                {s}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={tool} onValueChange={setTool}>
          <SelectTrigger>
            <SelectValue placeholder="Select tool" />
          </SelectTrigger>
          <SelectContent>
            {toolsForServer.map((t) => (
              <SelectItem key={t.tool} value={t.tool}>
                {t.tool}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {schema ? (
        <div>
          <p className="mb-1 text-sm text-muted-foreground">Input schema:</p>
          <pre className="max-h-48 overflow-y-auto rounded-md border bg-muted p-2 text-xs">
            {JSON.stringify(schema, null, 2)}
          </pre>
        </div>
      ) : null}
      <div className="space-y-1">
        <label className="text-sm font-medium">Arguments (JSON)</label>
        <Textarea
          rows={4}
          className="font-mono text-sm"
          value={args}
          onChange={(e) => setArgs(e.target.value)}
        />
      </div>
      <Button onClick={handleCall} disabled={!tool || callTool.isPending}>
        {callTool.isPending ? "Calling…" : "Call tool"}
      </Button>
      {callTool.data ? (
        <div>
          <p className="mb-1 text-sm text-muted-foreground">Result:</p>
          <pre
            className={`max-h-64 overflow-y-auto rounded-md border p-2 text-xs ${
              callTool.data.isError ? "border-destructive bg-destructive/10" : "bg-muted"
            }`}
          >
            {JSON.stringify(callTool.data.content, null, 2)}
          </pre>
        </div>
      ) : null}
      {callTool.isError ? (
        <p className="text-sm text-destructive">{callTool.error.message}</p>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 2: Point the route at the real page**

In `packages/webui/src/router.tsx`, add `import { ToolTesterPage } from "@/components/ToolTesterPage";` and change `testerRoute`'s `component` from the placeholder to `ToolTesterPage`.

- [ ] **Step 3: Verify**

```bash
cd packages/webui
pnpm exec tsc -b --noEmit
pnpm dev
```
Open `/admin/tester` with at least one running server — expected: server/tool dropdowns populate from `/api/tools`, selecting a tool shows its input schema, "Call tool" with valid JSON args returns a result rendered as formatted JSON (or a red error box if the call fails).

- [ ] **Step 4: Commit**

```bash
git add packages/webui/src
git commit -m "feat(webui): tool tester tab — pick server/tool, view schema, call with JSON args"
```

---

### Task 11: Multi-stage Dockerfile + backend static serving of the built SPA

**Files:**
- Modify: `packages/aggregator/Dockerfile`
- Modify: `packages/aggregator/src/aggregator/config.py`
- Modify: `packages/aggregator/src/aggregator/main.py`
- Create: `.dockerignore` (repo root)

**Interfaces:**
- Consumes: `WEBUI_DIST_DIR` (new config var), the built `packages/webui/dist/` output (Task 5-10).
- Produces: a working `docker compose build` that produces a single `mcp-aggregator` image serving the SPA under `/admin` and the API under `/api`.

- [ ] **Step 1: Add `WEBUI_DIST_DIR` to config**

In `packages/aggregator/src/aggregator/config.py`, add this line near the other path-related config (after `LOGS_DIR = DATA_DIR / "logs"`):
```python
WEBUI_DIST_DIR = Path(os.getenv("WEBUI_DIST_DIR", "webui_dist"))
```
Default is relative to the process's working directory — the Dockerfile below sets both `WORKDIR /app` and `ENV WEBUI_DIST_DIR=/app/webui_dist`, so in the container it resolves to `/app/webui_dist`. For local non-Docker testing of this task, run `uvicorn` from `packages/aggregator/` after copying `packages/webui/dist` to `packages/aggregator/webui_dist` (exactly what the root `build` script from Task 5 does).

- [ ] **Step 2: Add the SPA-serving route to `main.py`**

Add this at the very end of `packages/aggregator/src/aggregator/main.py` (after every other route in the file, so it never shadows `/admin/login/github`, `/admin/logout`, `/api/me`, or anything mounted via `api_router`/`oauth_router`):
```python
from fastapi.responses import FileResponse
from .config import WEBUI_DIST_DIR


@app.get("/admin")
@app.get("/admin/{path:path}")
async def admin_spa(path: str = ""):
    dist_root = WEBUI_DIST_DIR.resolve()
    candidate = (dist_root / path).resolve() if path else None
    if candidate and candidate.is_file() and dist_root in candidate.parents:
        return FileResponse(candidate)
    return FileResponse(dist_root / "index.html")
```
Add `from .config import WEBUI_DIST_DIR` to the existing `from .config import ADMIN_TOKEN, LOG_LEVEL` import near the top of the file instead of re-importing — change that line to:
```python
from .config import ADMIN_TOKEN, LOG_LEVEL, WEBUI_DIST_DIR
```
and drop the duplicate `from .config import WEBUI_DIST_DIR` from the snippet above (it's shown separately here only to make clear which name the route needs — the real edit adds one name to the existing import line, plus a fresh `from fastapi.responses import FileResponse` line, plus the route itself at the end of the file).

`dist_root in candidate.parents` guards against path traversal (e.g. `/admin/../../etc/passwd`) — `resolve()` normalizes both sides first so this check is reliable.

- [ ] **Step 3: Rewrite `packages/aggregator/Dockerfile`**

```dockerfile
# ── Stage 1: build the webui SPA ────────────────────────────────────────────
FROM node:22-slim AS webui-build

RUN corepack enable
WORKDIR /repo

COPY pnpm-workspace.yaml package.json ./
COPY packages/webui packages/webui

RUN pnpm install --filter webui
RUN pnpm --filter webui build

# ── Stage 2: aggregator runtime ─────────────────────────────────────────────
FROM python:3.12-slim

# System deps: git + curl (for uv) + Node.js LTS
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# uv (uvx for isolated PyPI MCP servers)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

COPY packages/aggregator/pyproject.toml .
COPY packages/aggregator/src ./src
RUN pip install --no-cache-dir .

COPY --from=webui-build /repo/packages/webui/dist ./webui_dist
ENV WEBUI_DIST_DIR=/app/webui_dist

RUN mkdir -p /data && chmod 777 /data

EXPOSE 8000
VOLUME ["/data"]

CMD ["uvicorn", "aggregator.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
```

- [ ] **Step 4: Add `.dockerignore` at the repo root**

```
.git
.venv
**/__pycache__
**/*.pyc
**/node_modules
packages/webui/dist
packages/aggregator/webui_dist
.env
.env.*
!.env.example
.remember
docs
```

- [ ] **Step 5: Verify**

```bash
docker compose build mcp-aggregator
```
Expected: both build stages complete (`webui-build` runs `pnpm install`/`pnpm build`, then the Python stage installs `aggregator` and copies the built assets in). This does **not** verify the container boots cleanly — recall the documented pre-existing `mcp` API bug in `aggregator.py` (see "Known pre-existing issue" at the top of this plan) will still crash the app at import time when `uvicorn` starts. If you want to confirm the static-serving route itself works in isolation, run inside the built image (or locally per Step 1's note):
```bash
python3 -c "
from pathlib import Path
import ast
src = Path('packages/aggregator/src/aggregator/main.py').read_text()
ast.parse(src)  # confirms the added route is syntactically valid
print('main.py parses OK')
"
```

- [ ] **Step 6: Commit**

```bash
git add packages/aggregator/Dockerfile packages/aggregator/src/aggregator/config.py packages/aggregator/src/aggregator/main.py .dockerignore
git commit -m "feat(aggregator): multi-stage Docker build serving the webui SPA under /admin"
```

---

### Task 12: Update README structure section

**Files:**
- Modify: `README.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Find and update the structure section**

```bash
grep -n "src/mcp_aggregator" README.md
```
Replace the old tree (rooted at `aggregator/.../src/mcp_aggregator/`) with one reflecting the new layout:
```
packages/
├── aggregator/            # Python backend (FastAPI), installed as `aggregator`
│   ├── src/
│   │   └── aggregator/
│   │       ├── main.py
│   │       ├── aggregator.py
│   │       ├── admin_auth.py
│   │       ├── oauth.py
│   │       ├── child_manager.py
│   │       ├── config.py
│   │       ├── database.py
│   │       ├── installer.py
│   │       ├── log_capture.py
│   │       ├── models.py
│   │       └── api/
│   │           ├── oauth_router.py
│   │           └── routers.py
│   ├── Dockerfile
│   └── pyproject.toml
└── webui/                 # React/TS/Vite admin SPA, served by aggregator under /admin
    └── src/
        ├── main.tsx
        ├── router.tsx
        ├── components/
        ├── hooks/
        └── lib/
```
Keep the rest of the README (install instructions, git-URL/subdirectory examples, etc.) as-is — those already describe `packages/my-server`-style subpaths generically and don't reference the old `aggregator/` path.

- [ ] **Step 2: Verify**

```bash
grep -n "aggregator/src/mcp_aggregator\|^aggregator/" README.md
```
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: update README structure section for the packages/ monorepo layout"
```
