# tiny-metamcp

Lightweight self-hosted MCP aggregator for Coolify. Aggregates MCP servers from PyPI, npm, and git repositories behind a single endpoint with GitHub OAuth authentication.

Inspired by [MetaMCP](https://github.com/metatool-ai/metatool-app).

## Architecture

```
MCP Client (Claude Web UI, Claude Desktop, etc.)
        │  OAuth 2.1 + PKCE  OR  Bearer ADMIN_TOKEN
        ▼
   Traefik (Coolify) — TLS / Let's Encrypt
        │
        ▼
   mcp-aggregator  (FastAPI + Python MCP SDK)
    ├── /mcp        SSE endpoint  ← MCP clients
    ├── /admin      Web UI        ← browser (GitHub login)
    ├── /api        REST API      ← browser session or Bearer token
    └── /health     Health check  ← Docker
         │
         ├── child: uvx <package>       (PyPI)
         ├── child: npx <package>       (npm)
         └── child: git clone + run    (git)
```

**Auth model:**
- **Browser → `/admin`, `/api`** — GitHub OAuth via signed session cookies. Only usernames in `GITHUB_ALLOWED_USERS` are allowed in.
- **MCP client → `/mcp`** — OAuth 2.1 + PKCE (Claude Web UI connectors) or static `ADMIN_TOKEN` bearer token (Claude Desktop etc.).
- **Claude Web UI** — Full OAuth 2.1 flow: discovery → dynamic client registration → PKCE authorize → GitHub login → token exchange. No manual token needed.

## Prerequisites

- Docker + Docker Compose
- [`just`](https://github.com/casey/just) (`cargo install just` or `brew install just`)
- A GitHub OAuth App (see setup below)

## Getting Started

### 1. Clone and generate environment variables

```bash
git clone <repo-url>
cd custom-mcp-meta

# Non-interactive: generates secrets, sets CHANGE_ME for the rest
just init-env

# Interactive: prompts for domain and GitHub values
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

### 3. Start

```bash
just up     # build and start in the background
just logs   # follow output
```

---

## Local Testing

For local development, `docker-compose.override.yml` is merged automatically. It is in `.gitignore` and won't reach Coolify. The provided `docker-compose.override.yml` exposes port 8000 directly.

For a local GitHub OAuth App, set the callback URL to `http://localhost:8000/admin/callback` (browser login) and `http://localhost:8000/oauth/callback` (MCP OAuth). Or register a second app for local use only.

```bash
just up

# 1. Service status
just ps
just health

# 2. MCP bearer token access
TOKEN=$(grep ^ADMIN_TOKEN .env | cut -d= -f2)
curl -sI http://localhost:8000/mcp                                       # → 401
curl -H "Authorization: Bearer $TOKEN" --max-time 2 http://localhost:8000/mcp  # → SSE stream

# 3. REST API
curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/servers | python3 -m json.tool

# 4. Full MCP protocol test
npx @modelcontextprotocol/inspector
# URL: http://localhost:8000/mcp  |  Header: Authorization: Bearer <ADMIN_TOKEN>
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
        "Authorization": "Bearer <ADMIN_TOKEN>"
      }
    }
  }
}
```

---

## Administration

### Web UI

Go to `https://<MCP_DOMAIN>/admin` — sign in with GitHub. From here you can:

- **MCP Servers** — add, enable/disable, restart, and delete servers
- **Logs** — view aggregator logs and child process stderr in real time (live SSE stream)
- **Tool Tester** — select a running server and tool, fill in JSON arguments, and call it directly

### REST API

All API endpoints require authentication: either a valid admin session cookie (browser) or `Authorization: Bearer <ADMIN_TOKEN>`.

```bash
BASE=https://<MCP_DOMAIN>
TOKEN=<ADMIN_TOKEN>

# List servers
curl -H "Authorization: Bearer $TOKEN" $BASE/api/servers | jq

# Add a server
curl -X POST $BASE/api/servers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"<name>","type":"pypi|npm|git|cmd","package":"<package>","args":[],"env":{}}'

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

### Overview

| Type | How it runs | Best for |
|------|-------------|----------|
| `pypi` | `uvx` (isolated) | Python MCP servers from PyPI or git |
| `npm` | `npx` (cached) | Node.js/TS from npm or GitHub |
| `git` | clone → run locally | Unpublished Python repos |
| `cmd` | none | Locally installed binaries |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `MCP_DOMAIN` | ✅ | Public hostname (without `https://`) |
| `ADMIN_TOKEN` | — | Static bearer token for MCP clients. Generate: `openssl rand -hex 32` |
| `SESSION_SECRET` | ✅ | Signing key for admin session cookies. Generate: `openssl rand -base64 32` |
| `GITHUB_CLIENT_ID` | ✅ | GitHub OAuth App Client ID |
| `GITHUB_CLIENT_SECRET` | ✅ | GitHub OAuth App Client Secret |
| `GITHUB_ALLOWED_USERS` | ✅ | Comma-separated list of allowed GitHub usernames |
| `LOG_LEVEL` | — | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default: `INFO`) |

---

## Project Structure

```
.
├── docker-compose.yml           Production setup (mcp-aggregator + Traefik labels)
├── docker-compose.override.yml  Local override with exposed ports (not in git)
├── Justfile                     Command shortcuts
├── scripts/
│   └── init-env.sh              Generates .env with auto-generated secrets
├── .env.example                 Environment variable template
└── aggregator/
    ├── Dockerfile               Python 3.12 + uv + Node.js LTS + git
    ├── pyproject.toml
    └── src/mcp_aggregator/
        ├── main.py              FastAPI app, lifespan, routes
        ├── aggregator.py        MCP SSE server + tool aggregation
        ├── child_manager.py     Process lifecycle for child servers
        ├── installer.py         uvx / npx / git clone + npm build
        ├── database.py          SQLite via SQLModel + aiosqlite
        ├── models.py            SQLModel ORM models
        ├── admin_auth.py        GitHub OAuth browser login + signed session cookies
        ├── oauth.py             OAuth 2.1 + PKCE authorization server
        ├── config.py            Environment variable settings
        ├── log_capture.py       In-memory log buffer + SSE pub-sub
        ├── templates/           Jinja2 templates (pico.css + HTMX + Alpine.js)
        └── api/
            ├── routers.py       REST API
            └── oauth_router.py  OAuth 2.1 endpoints
```

## Just Commands

```
just init-env          Generate .env (secrets auto-generated, rest CHANGE_ME)
just init-env -i       Interactive mode — prompts for all values
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
