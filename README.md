# tiny-metamcp

Lightweight self-hosted MCP aggregator for Coolify. Aggregates MCP servers from PyPI, npm, git repositories, and remote HTTP servers behind a single endpoint with GitHub or Steam authentication.

Inspired by [MetaMCP](https://github.com/metatool-ai/metatool-app).

## Architecture

```
MCP Client (Claude Web UI, Claude Desktop, etc.)
        │  OAuth 2.1 + PKCE  OR  Bearer <personal token>
        ▼
   Traefik (Coolify) — TLS / Let's Encrypt
        │
        ▼
   mcp-aggregator  (FastAPI + Python MCP SDK)
    ├── /mcp        SSE endpoint  ← MCP clients
    ├── /admin      Web UI        ← browser (GitHub or Steam login)
    ├── /api        REST API      ← browser session or Bearer token
    └── /health     Health check  ← Docker
         │
         ├── child: uvx <package>       (PyPI)
         ├── child: npx <package>       (npm)
         └── child: git clone + run    (git)
```

**Auth model:**
- **Browser → `/admin`, `/api`** — GitHub or Steam login via signed session cookies (whichever is configured; both may be enabled at once). Only allowed identities (`GITHUB_ALLOWED_USERS` / `STEAM_ALLOWED_USERS`) get in.
- **MCP client → `/mcp`** — OAuth 2.1 + PKCE (Claude Web UI connectors, choosing a provider if more than one is configured) or a personal API token (Claude Desktop etc. — generate one from the webui's Account page after logging in).
- **Claude Web UI** — Full OAuth 2.1 flow: discovery → dynamic client registration → PKCE authorize → provider login → token exchange. No manual token needed.
- **Identity across providers** — a GitHub login and a Steam login are always separate identities in this system (`"github:octocat"` vs `"steam:76561198012345678"`) — there's no account linking. `ADMIN_USERS` values must include the provider prefix.
- **Account linking** — while logged in, link a second provider from the Account page (self-service; both identities must be logged into directly, no admin override). Linked identities reach the same account either way.

## Upgrading

Upgrading an existing (pre-Steam-login) deployment to a version with Steam
login requires one manual change and involves a couple of automatic,
one-time effects:

- **`ADMIN_USERS` must be updated to the prefixed format.** An existing
  entry like `octocat` must become `github:octocat`. This is not enforced
  at startup — an unprefixed entry simply stops matching and silently
  demotes that admin to a regular user, with no error logged. Update your
  `.env` (or Coolify environment variables) before or immediately after
  upgrading.
- **`GITHUB_ALLOWED_USERS` is unchanged** — its values stay unprefixed raw
  GitHub usernames, no action needed.
- **Server ownership and personal API tokens migrate automatically.**
  `servers.owner_username` and `personal_tokens.username` are backfilled
  with the `github:` prefix on first startup after upgrade (every existing
  identity was necessarily a GitHub one, since GitHub was the only
  provider before). No manual steps needed — existing owners keep managing
  their servers and existing personal tokens keep working.
- **The GitHub OAuth App registration is unchanged** — the callback URL
  (`/oauth/callback`) is the same as before, so no re-registration is
  needed.
- **Existing admin browser sessions will be logged out once.** Session
  cookies issued before the upgrade predate the new cookie payload shape
  and are treated as invalid on first visit after upgrading. Logging in
  again resolves this — it's expected, not a bug.
- **Allow-lists and `ADMIN_USERS` become one-time seed values.** On first startup after upgrading, `GITHUB_ALLOWED_USERS`/`STEAM_ALLOWED_USERS`/`ADMIN_USERS` are read once into the database and then have **no further effect** — manage allow-lists and admin rights from the webui's Users page from then on. Editing these in `.env`/Coolify after the first startup does nothing.

## Prerequisites

- Docker + Docker Compose
- [`just`](https://github.com/casey/just) (`cargo install just` or `brew install just`)
- A GitHub OAuth App and/or a Steam Web API key (see setup below) — at least one is required

## Getting Started

### 1. Clone and generate environment variables

```bash
git clone <repo-url>
cd tiny-metamcp

# Non-interactive: generates secrets, sets CHANGE_ME for the rest
just init-env

# Interactive: prompts for domain, GitHub, and Steam values
just init-env -i
```

### 2. Register a GitHub OAuth App

Go to **github.com → Settings → Developer settings → OAuth Apps → New OAuth App**:

| Field | Value |
|-------|-------|
| Application name | `tiny-metamcp` (or any name) |
| Homepage URL | `https://<MCP_DOMAIN>` |
| Authorization callback URL | `https://<MCP_DOMAIN>/oauth/callback` |

> Both the admin browser login and the MCP client OAuth flow share the same callback path. The app routes them internally based on a cookie set before the GitHub redirect.

Copy the **Client ID** and **Client Secret** into `.env`.

### 2b. (Optional) Get a Steam Web API key

Steam login is an alternative to GitHub — configure either one, or both.

Go to **steamcommunity.com/dev/apikey**, sign in, and request a key (any
domain name works for the "Domain Name" field, it's not validated
strictly). Copy the key into `.env` as `STEAM_API_KEY`.

Unlike GitHub's OAuth App, Steam's OpenID 2.0 login needs no callback URL
registration — it works immediately once `STEAM_API_KEY` is set.

### 3. Start

```bash
just up     # build and start in the background
just logs   # follow output
```

---

## Local Testing

For local development, `docker-compose.override.yml` is merged automatically. It is in `.gitignore` and won't reach Coolify. The provided `docker-compose.override.yml` exposes port 8000 directly.

For a local GitHub OAuth App, set the callback URL to `http://localhost:8000/oauth/callback` — this single path handles both the admin browser login and the MCP client OAuth flow (see the setup step above). Or register a second app for local use only.

```bash
just up

# 1. Service status
just ps
just health

# 2. MCP bearer token access — generate a personal token first: log into
#    the webui at http://localhost:8000/admin, open "Account", click
#    "Generate token", then:
TOKEN=<paste the generated token>
curl -sI http://localhost:8000/mcp                                       # → 401
curl -H "Authorization: Bearer $TOKEN" --max-time 2 http://localhost:8000/mcp  # → SSE stream

# 3. REST API
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/servers | python3 -m json.tool

# 4. Full MCP protocol test
npx @modelcontextprotocol/inspector
# URL: http://localhost:8000/mcp  |  Header: Authorization: Bearer <your personal token>
```

---

## Coolify Deployment

1. Create a new project in Coolify → **Add Resource → Docker Compose**
2. Connect to the GitHub repository
3. **Compose file path:** `docker-compose.yml`
4. Add environment variables from `.env`
5. Click **Deploy** — Coolify builds the `mcp-aggregator` image and starts the service

> Do not include `docker-compose.override.yml` in Coolify. It is in `.gitignore` and will not be available there.

---

## Configuring MCP Clients

**Claude Web UI** — no manual configuration needed. Add the connector URL `https://<MCP_DOMAIN>/mcp` and Claude will discover OAuth automatically and prompt for login.

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "tiny-metamcp": {
      "url": "https://<MCP_DOMAIN>/mcp",
      "headers": {
        "Authorization": "Bearer <your personal token>"
      }
    }
  }
}
```

---

## Administration

### Web UI

Go to `https://<MCP_DOMAIN>/admin` — sign in with GitHub or Steam (whichever is configured). From here you can:

- **MCP Servers** — add, edit, enable/disable, restart, and delete servers
- **Logs** — view aggregator logs and child process stderr in real time (live SSE stream)
- **Tool Tester** — select a running server and tool, fill in JSON arguments, and call it directly
- **Account** — view your username/admin status, generate a personal API token for MCP clients (Claude Desktop etc.)

### User management (admins)

The **Users** nav link (admin-only) shows every account, its linked
identities, and toggles for admin rights and whether the account may still
log in. A second section manages the "pending identities" allow-list — add
a raw GitHub login or SteamID64 there to pre-approve someone before they've
ever logged in (optionally granting admin immediately). Leave a provider's
allow-list empty to let anyone with that provider sign in, same as the old
env-var default.

### REST API

All API endpoints require authentication: either a valid admin session cookie (browser) or `Authorization: Bearer <personal token>`.

```bash
BASE=https://<MCP_DOMAIN>
TOKEN=<your personal token>

# List servers
curl -H "Authorization: Bearer $TOKEN" $BASE/api/servers | jq

# To generate/regenerate your personal token, use the webui's Account page
# instead (see "Web UI" above) — /api/me/token only accepts an active
# browser session cookie, not a bearer token, so it can't be called with
# curl like the endpoints below.

# Add a server
curl -X POST $BASE/api/servers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"<name>","type":"pypi|npm|git|cmd|proxy","package":"<package>","args":[],"env":{},"visibility":"private|everyone"}'

# Edit a server (partial update — only send the fields you want to change)
curl -X PATCH $BASE/api/servers/<id> \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"env":{"KEY":"value"}}'

# Enable / disable
curl -X POST -H "Authorization: Bearer $TOKEN" $BASE/api/servers/<id>/enable
curl -X POST -H "Authorization: Bearer $TOKEN" $BASE/api/servers/<id>/disable

# Restart
curl -X POST -H "Authorization: Bearer $TOKEN" $BASE/api/servers/<id>/restart

# Delete
curl -X DELETE -H "Authorization: Bearer $TOKEN" $BASE/api/servers/<id>

# List all tools (including inputSchema)
curl -H "Authorization: Bearer $TOKEN" $BASE/api/tools | jq

# Call a tool
curl -X POST $BASE/api/tools/call \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"server":"<server-name>","tool":"<tool-name>","arguments":{"key":"value"}}'

# Logs (last 200 entries)
curl -H "Authorization: Bearer $TOKEN" "$BASE/api/logs" | jq
curl -H "Authorization: Bearer $TOKEN" "$BASE/api/logs?server=<server-name>" | jq

# Child process stderr
curl -H "Authorization: Bearer $TOKEN" "$BASE/api/logs/<server-name>/stderr" | jq

# SSE live stream (blocking — use with curl --no-buffer)
curl --no-buffer -H "Authorization: Bearer $TOKEN" "$BASE/api/logs/stream"
curl --no-buffer -H "Authorization: Bearer $TOKEN" "$BASE/api/logs/stream?server=<server-name>"
```

### MCP Meta Tools

Beyond the REST API and web UI, the server registry can also be managed as **plain MCP tools**, callable by any client already authenticated to `/mcp` — no separate credentials, no REST calls. Useful for letting an LLM (Claude Desktop, Claude Web UI) manage its own server list directly from within a conversation.

| Tool | Arguments | Description |
|------|-----------|-------------|
| `list_servers` | — | List all configured servers with status |
| `add_server` | `name`, `type`, `package`, `args?`, `env?`, `visibility?` | Add and start a new server |
| `edit_server` | `name`, `new_name?`, `type?`, `package?`, `args?`, `env?`, `visibility?` | Edit an existing server's configuration (`name` identifies the server; only the other fields you provide are changed) |
| `delete_server` | `name` | Stop and permanently remove a server |
| `enable_server` | `name` | Enable and start a disabled server |
| `disable_server` | `name` | Stop and disable a server without deleting it |
| `restart_server` | `name` | Restart a running server |

These tools are unprefixed — proxied tools are always namespaced `<server>__<tool>`, so there's no collision surface.

> **Access model:** each server has an owner (whoever added it) and a
> visibility (`everyone` or `private`). A caller can see and use
> (`list_servers`, tool calls) any `everyone`-visibility server plus
> their own `private` ones. Only the owner or an admin (`ADMIN_USERS` —
> prefixed identities like `github:octocat` or `steam:76561198012345678`)
> can manage a server (`edit_server`/`delete_server`/`enable_server`/
> `disable_server`/`restart_server`). `env` values returned by
> `list_servers` are redacted (`***`); variable names are visible but
> not their values, since this output can land in an LLM's conversation
> history rather than staying on the admin-only REST/web UI surface.

---

## Server Types and Configuration

Tools from child servers are namespaced as `<server-name>__<tool-name>` to avoid conflicts.

### `pypi` — Python packages via uvx

Used for Python MCP servers. `uvx` isolates the package and handles dependencies automatically.

**Simple PyPI package:**

| Field | Value |
|-------|-------|
| Name | `fetch` |
| Type | `pypi` |
| Package | `mcp-server-fetch` |
| Args | — |

```bash
curl -X POST $BASE/api/servers \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"fetch","type":"pypi","package":"mcp-server-fetch"}'
```

**PyPI package where the console script name differs from the package name** (args[0] = entrypoint):

| Field | Value |
|-------|-------|
| Package | `markitdown-mcp` |
| Args | `markitdown-mcp` |

Runs: `uvx --from markitdown-mcp markitdown-mcp`

**Git URL (single repo):**

| Field | Value |
|-------|-------|
| Package | `git+https://github.com/org/repo` |
| Args | `entrypoint-name` |

Runs: `uvx --from git+https://github.com/org/repo entrypoint-name`

**Git URL, monorepo with subpackage (`#subdirectory=`):**

| Field | Value |
|-------|-------|
| Package | `git+https://github.com/org/repo#subdirectory=packages/my-server` |
| Args | `my-server` |

Runs: `uvx --from git+https://...#subdirectory=packages/my-server my-server`

**Private repo** — token in package URL:

| Field | Value |
|-------|-------|
| Package | `git+https://<token>@github.com/org/repo#subdirectory=packages/my-server` |

Or pass the token as an environment variable:

```json
{
  "name": "my-server", "type": "pypi",
  "package": "git+https://github.com/org/repo#subdirectory=packages/my-server",
  "args": ["my-server"],
  "env": {"GIT_ASKPASS": "echo", "GITHUB_TOKEN": "<token>"}
}
```

---

### `npm` — Node.js/TypeScript packages via npx

Used for Node.js and TypeScript MCP servers. `npx --yes` downloads and runs the package directly.

**Published npm package:**

| Field | Value |
|-------|-------|
| Name | `filesystem` |
| Type | `npm` |
| Package | `@modelcontextprotocol/server-filesystem` |
| Args | `/allowed/folder` |

```bash
curl -X POST $BASE/api/servers \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"filesystem","type":"npm","package":"@modelcontextprotocol/server-filesystem","args":["/data"]}'
```

**TypeScript repo from GitHub (not published to npm):**

| Field | Value |
|-------|-------|
| Package | `git+https://github.com/org/ts-mcp-server` |
| Args | — |

Runs: `npx --yes git+https://github.com/org/ts-mcp-server`

npm clones the repo, runs `npm install` and the `prepare` script (TypeScript compilation), and starts the binary from the `bin` field in `package.json`. Requires the package to have a correct `prepare` script and `bin` entry.

**GitHub shorthand and other npm-supported URL formats:**

```
git+https://github.com/org/repo     # full HTTPS
git+ssh://git@github.com/org/repo   # SSH
github:org/repo                     # GitHub shorthand
```

**Private TypeScript repo:**

| Field | Value |
|-------|-------|
| Package | `git+https://<token>@github.com/org/ts-mcp-server` |

---

### `git` — clone and run locally

Clones the entire repo to `/data/packages/<name>` and runs it from there. Primarily used for Python repos not published to PyPI.

- **Python repo** (`pyproject.toml` / `setup.py`): run via `uvx --from <clone-dir>`
- **Node.js repo** (`package.json`): run via `node <main>` after automatic `npm install` and `npm run build`

| Field | Value |
|-------|-------|
| Name | `my-server` |
| Type | `git` |
| Package | `https://github.com/org/repo` |
| Args | optional entrypoint (Python) |

```bash
curl -X POST $BASE/api/servers \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"my-server","type":"git","package":"https://github.com/org/repo"}'
```

> For TypeScript repos from GitHub, `npm` mode with a `git+https://` URL is simpler — npm caches globally and avoids local cloning.

---

### `cmd` — direct command

Runs an arbitrary command. Useful for locally installed servers or custom scripts.

| Field | Value |
|-------|-------|
| Name | `my-tool` |
| Type | `cmd` |
| Package | `/usr/local/bin/my-mcp-server` |
| Args | `--config /data/config.json` |

The package field is split on spaces and joined with args: `/usr/local/bin/my-mcp-server --config /data/config.json`

---

### `proxy` — connect to a remote HTTP server

Connects to an already-running MCP server via Streamable HTTP (SSE) instead of spawning a local subprocess. Useful for connecting to MCP servers running on other machines or in other containers.

| Field | Value |
|-------|-------|
| Name | `remote-mcp` |
| Type | `proxy` |
| Package | `http://localhost:3000/mcp` |
| Args | — (unused) |

The `package` field contains the full HTTP URL of the remote MCP server's SSE endpoint. No authentication is currently supported for proxy connections.

```bash
curl -X POST $BASE/api/servers \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"remote-mcp","type":"proxy","package":"http://mcp-server-host:3000/mcp"}'
```

---

### Overview

| Type | How it runs | Best for |
|------|-------------|----------|
| `pypi` | `uvx` (isolated) | Python MCP servers from PyPI or git |
| `npm` | `npx` (cached) | Node.js/TS from npm or GitHub |
| `git` | clone → run locally | Unpublished Python repos |
| `cmd` | none | Locally installed binaries |
| `proxy` | HTTP SSE connection | Remote MCP servers |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `MCP_DOMAIN` | ✅ | Public hostname (without `https://`) |
| `SESSION_SECRET` | ✅ | Signing key for admin session cookies. Generate: `openssl rand -base64 32` |
| `GITHUB_CLIENT_ID` | — * | GitHub OAuth App Client ID |
| `GITHUB_CLIENT_SECRET` | — * | GitHub OAuth App Client Secret |
| `GITHUB_ALLOWED_USERS` | — | Comma-separated list of allowed GitHub usernames (unprefixed) |
| `STEAM_API_KEY` | — * | Steam Web API key — its presence enables Steam login |
| `STEAM_ALLOWED_USERS` | — | Comma-separated list of allowed raw SteamID64s (unprefixed) |
| `ADMIN_USERS` | — | Comma-separated **prefixed** identities with admin rights (e.g. `github:octocat,steam:76561198012345678`) |
| `LOG_LEVEL` | — | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default: `INFO`) |

\* At least one of (`GITHUB_CLIENT_ID` + `GITHUB_CLIENT_SECRET`) or `STEAM_API_KEY` is required — the app refuses to start with neither configured.

---

## Development

### Backend (Python / uv)

```bash
cd packages/aggregator
uv sync                    # installs runtime + dev deps (pytest, pytest-asyncio)
uv run uvicorn aggregator.main:app --reload --port 8000
```

`uv sync` at the repo root (or inside `packages/aggregator`) installs the `dev` dependency group automatically — no extra flag needed.

### Frontend (React / pnpm)

```bash
pnpm install
just webui-dev               # (or: pnpm dev:webui) Vite dev server on :5173, proxying /api, /admin/login*, /oauth/* to :8000
```

Run the backend (above) and the frontend dev server side by side — the Vite proxy makes them behave as one origin during development, so cookie-based admin auth works without any CORS setup. `pnpm build` (from the repo root) builds the webui and copies it into `packages/aggregator/webui_dist`, matching what the Docker image serves in production.

### Linting and formatting

Python code is linted and formatted with [ruff](https://docs.astral.sh/ruff/) (config in `packages/aggregator/pyproject.toml`). A pre-commit hook runs `ruff check --fix` and `ruff format` automatically on every commit touching `packages/aggregator/`:

```bash
uv tool install pre-commit   # once, globally
pre-commit install           # once, per clone — activates the git hook
```

Run manually without committing: `just lint` / `just format`.

### Tests

```bash
just test
# or directly:
cd packages/aggregator && uv run pytest
```

---

## Project Structure

```
tiny-metamcp/
├── packages/
│   ├── aggregator/            # Python backend (FastAPI), installed as `aggregator`
│   │   ├── src/
│   │   │   └── aggregator/
│   │   │       ├── main.py
│   │   │       ├── aggregator.py
│   │   │       ├── admin_auth.py
│   │   │       ├── oauth.py
│   │   │       ├── child_manager.py
│   │   │       ├── config.py
│   │   │       ├── database.py
│   │   │       ├── installer.py
│   │   │       ├── log_capture.py
│   │   │       ├── meta_tools.py     # native MCP tools for server management
│   │   │       ├── models.py
│   │   │       └── api/
│   │   │           ├── oauth_router.py
│   │   │           └── routers.py
│   │   ├── tests/              # pytest suite (child_manager, meta_tools)
│   │   ├── Dockerfile
│   │   └── pyproject.toml      # deps, ruff config, pytest config
│   └── webui/                  # React/TS/Vite admin SPA, served by aggregator under /admin
│       └── src/
│           ├── main.tsx
│           ├── router.tsx
│           ├── components/
│           ├── hooks/
│           └── lib/
├── scripts/
│   └── init-env.sh
├── docker-compose.yml
├── Justfile
├── pyproject.toml              # uv workspace root
├── pnpm-workspace.yaml
└── .pre-commit-config.yaml
```

## Just Commands

```
just init-env          Generate .env (secrets auto-generated, rest CHANGE_ME)
just init-env -i       Interactive mode — prompts for all values
just test              Run the aggregator's pytest suite
just lint              Check aggregator Python code with ruff
just format            Format aggregator Python code with ruff
just webui-dev         Start the webui Vite dev server
just up                Build and start in the background
just dev               Start in the foreground with live logs
just down              Stop all services
just down-volumes      Stop and delete all data (asks for confirmation)
just build             Build images without starting
just restart           Restart all services
just restart <name>    Restart one service
just logs              Follow all logs
just logs <name>       Follow one service
just ps                Show container status
just health            Check aggregator health endpoint
```
