# Monorepo restructuring + React admin webui

**Date:** 2026-08-02
**Status:** Approved

## Context

The repo is mid-restructure from a single `aggregator/` project into a uv/pnpm
monorepo under `packages/`. `aggregator/` has already been deleted and its
contents moved to `packages/aggregator/`, but the move is incomplete and left
the build broken:

- `packages/aggregator/src/*.py` is flat (no `mcp_aggregator/` package dir),
  and `pyproject.toml` has no `[build-system]` — `uv sync` cannot build it.
- `Dockerfile` CMD still references `mcp_aggregator.main:app`.
- `docker-compose.yml` build context still points at `./aggregator`.
- `README.md`'s structure section still describes the old layout.

Root `pyproject.toml` (uv workspace, `members = ["packages/*"]`) and root
`package.json` (`packageManager: pnpm@10.33.0`) are already in place.

Separately, the aggregator currently ships a server-rendered admin panel
(Jinja2 + htmx + Alpine.js, `packages/aggregator/src/templates/admin/`) with
three tabs: Servers (list/add/enable/disable/restart/delete), Logs (SSE
stream, filterable), and Tool Tester (call any tool with JSON args). This is
being replaced with a new `packages/webui` package: a React/TS/Vite SPA.

Investigation found the admin panel already talks to a complete JSON API
under `/api/*` (`packages/aggregator/src/api/routers.py`) for almost
everything — servers CRUD, tools list, tool call, logs + SSE stream. Only a
handful of `/admin/*` routes in `main.py` return HTML fragments for
htmx and can be deleted outright once the SPA replaces them. Auth
(`admin_auth.py`, GitHub OAuth → signed session cookie) stays as-is.

## Decisions

1. **Package layout:** keep `packages/aggregator/src/*.py` flat (no nested
   package dir). Install it under import name `aggregator` via hatchling
   source-mapping, not literally `src` (`src` as a top-level import name
   risks colliding with other future workspace packages laid out the same
   way). `Dockerfile` CMD becomes `aggregator.main:app`.
2. **SPA serving:** FastAPI serves the built SPA directly via `StaticFiles`
   under `/admin` — one container, no CORS, cookie auth keeps working
   same-origin. No new docker-compose service, no Traefik changes.
3. **Styling:** Tailwind CSS + shadcn/ui.
4. **Data fetching:** TanStack Query for `/api/servers` and `/api/tools`,
   with cache invalidation after mutations (add/enable/disable/restart/delete).
   Logs stay on a raw `EventSource` against `/api/logs/stream` (SSE doesn't
   fit the query-cache model).
5. **Routing:** TanStack Router with real client routes: `/admin`,
   `/admin/logs`, `/admin/tester`, `/admin/login`.
6. **Login:** moved into the SPA (`/admin/login` route) rather than staying
   a separate server-rendered page. The SPA determines auth state via a new
   `GET /api/me` endpoint (200 + `{"username": ...}` or 401 — no redirect).

## Backend changes

- `packages/aggregator/pyproject.toml`: add `[build-system]` (hatchling),
  map `src` → import name `aggregator`.
- `Dockerfile`: multi-stage — stage 1 builds `packages/webui` with
  `pnpm build`, stage 2 is the existing Python image; copies the built
  static assets in and mounts them under `/admin`; CMD module path fixed to
  `aggregator.main:app`.
- `docker-compose.yml`: build context `./aggregator` → `./packages/aggregator`.
- `main.py`:
  - Add `GET /api/me` (session-cookie only; 401 on no/invalid session, no
    redirect) to `api/routers.py` or a small new router.
  - Delete the HTML-fragment routes: `GET /admin`, `GET /admin/servers-table`,
    `POST /admin/add-server`, `POST /admin/servers/{id}/enable`,
    `POST /admin/servers/{id}/disable`, `POST /admin/servers/{id}/restart`,
    `DELETE /admin/servers/{id}`.
  - Add a catch-all: any `GET /admin` or `GET /admin/*` not matching a
    static asset returns the built SPA's `index.html` (200, regardless of
    auth — the SPA itself gates on `/api/me`).
  - Keep `GET /admin/login`, `GET /admin/login/github`, `GET /admin/logout`,
    `GET /oauth/callback` (shared with MCP OAuth) unchanged. Success still
    redirects to `/admin`; failure to `/admin/login?error=...` (now a client
    route, still reads the query param).
- Delete `packages/aggregator/src/templates/` entirely (`admin/index.html`,
  `admin/_servers_table.html`, `admin/_add_result.html`, `admin/login.html`,
  `base.html`) and the `Jinja2Templates` wiring in `main.py`.
- `README.md`: update structure section for `packages/aggregator`,
  `packages/webui`.

## Frontend: `packages/webui`

- Vite + React + TypeScript.
- Tailwind CSS + shadcn/ui (Table, Tabs, Dialog, Badge, etc. — matches
  existing admin panel's use of badges for server status, tabs for the
  three sections).
- TanStack Router, TanStack Query.
- Feature parity with the current Alpine/htmx panel:
  - **Servers tab:** list with status badges (running/tool count/error),
    add-server form (name, type [pypi/npm/git/cmd], package, args, env),
    enable/disable/restart/delete actions.
  - **Logs tab:** live SSE stream from `/api/logs/stream`, filter by server
    and level, connect/disconnect indicator, auto-scroll.
  - **Tool Tester tab:** pick server → tool, view input schema, JSON args
    textarea, call tool, show result/error.
  - **Login route:** GitHub OAuth button (links to `/admin/login/github`,
    unchanged redirect flow), shows `?error=` message if present.
- Dev workflow: `vite dev` with a proxy for `/api`, `/admin/login*`,
  `/oauth/*` → `http://localhost:8000`, so the FastAPI backend and Vite dev
  server run side by side with hot reload during development.

## Root monorepo wiring

- `pnpm-workspace.yaml`: `packages: ["packages/*"]`.
- Root `package.json`: add a `build` script that runs the webui build.
- Root `pyproject.toml`, `.python-version`: already correct, no change.

## Out of scope

- No changes to MCP protocol endpoints (`/mcp`, `/messages`), OAuth 2.1
  client flow (`api/oauth_router.py`), child process management, database
  schema, or installer logic.
- No new docker-compose services or Traefik routing changes.
- No test suite is being added as part of this change (none exists today).
