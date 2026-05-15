# tiny-metamcp

Lettvekts MCP-aggregator for self-hosted Coolify-deployment. Samler MCP-servere fra PyPI, npm og git-repoer bak ett enkelt endepunkt med GitHub OAuth-beskyttelse.

Inspirert av [MetaMCP](https://github.com/metatool-ai/metatool-app), men uten stabilitets­problemene.

## Arkitektur

```
Klient (Claude Desktop e.l.)
        │  Bearer ADMIN_TOKEN
        ▼
   Traefik (Coolify) ── TLS / Let's Encrypt
        │
        ▼
   oauth2-proxy ────── GitHub OAuth (nettleser)
        │  alle ruter
        ▼
   mcp-aggregator  (FastAPI + Python MCP SDK)
    ├── /mcp        SSE-endepunkt  ← MCP-klienter
    ├── /admin      Web UI         ← nettleser (GitHub-innlogget)
    ├── /api        REST API       ← nettleser (GitHub-innlogget)
    └── /health     Helsesjekk     ← Docker
         │
         ├── child: uvx <pakke>        (PyPI)
         ├── child: npx <pakke>        (npm)
         └── child: git clone + run    (git)
```

**Auth-modell:**
- **Nettleser → `/admin`, `/api`** — GitHub OAuth via oauth2-proxy. Kun brukernavn i `GITHUB_ALLOWED_USERS` slipper inn.
- **MCP-klient → `/mcp`** — Bearer-token (`ADMIN_TOKEN`). oauth2-proxy slipper disse gjennom uten OAuth-sjekk.

## Forutsetninger

- Docker + Docker Compose
- [`just`](https://github.com/casey/just) (`cargo install just` eller `brew install just`)
- GitHub OAuth-app (se oppsett under)

## Kom i gang

### 1. Klon og generer miljøvariabler

```bash
git clone <repo-url>
cd custom-mcp-meta

# Ikke-interaktiv: genererer secrets, setter CHANGE_ME for resten
just init-env

# Interaktiv: spør om domene og GitHub-verdier
just init-env -i
```

### 2. Registrer GitHub OAuth-app

Gå til **github.com → Settings → Developer settings → OAuth Apps → New OAuth App**:

| Felt | Verdi |
|------|-------|
| Application name | `tiny-metamcp` (eller valgfritt) |
| Homepage URL | `https://<MCP_DOMAIN>` |
| Callback URL | `https://<MCP_DOMAIN>/oauth2/callback` |

Kopier **Client ID** og **Client secret** inn i `.env`.

### 3. Start

```bash
just up     # bygger og starter i bakgrunnen
just logs   # følg output
```

---

## Lokal testing

For lokal kjøring trenger oauth2-proxy en dedikert GitHub OAuth-app med `http://localhost:4180/oauth2/callback` som callback-URL, og `OAUTH2_PROXY_COOKIE_SECURE=false` i `.env` (siden ingen HTTPS lokalt).

Docker Compose merger automatisk `docker-compose.override.yml` med hoved-compose-filen. Opprett filen lokalt (den er i `.gitignore` og følger ikke med til Coolify):

```yaml
# docker-compose.override.yml
services:
  oauth2-proxy:
    ports:
      - "4180:4180"

  mcp-aggregator:
    ports:
      - "8000:8000"
```

`.env`-tillegg for lokal kjøring:

```bash
# Separat GitHub OAuth-app for lokal test:
GITHUB_CLIENT_ID=<lokal-test-app-id>
GITHUB_CLIENT_SECRET=<lokal-test-app-secret>

# Påkrevd uten HTTPS:
OAUTH2_PROXY_COOKIE_SECURE=false
```

### Testsekvens

```bash
just up

# 1. Tjenestestatus
just ps
just health

# 2. Auth-routing
curl -sI http://localhost:4180/admin | grep -E "HTTP|ocation"  # → 302 til GitHub
curl -sI http://localhost:4180/mcp   | grep HTTP               # → 200 (ikke redirect)

# 3. MCP Bearer-token
TOKEN=$(grep ^ADMIN_TOKEN .env | cut -d= -f2)
curl -sI http://localhost:4180/mcp                                     # → 401
curl -H "Authorization: Bearer $TOKEN" --max-time 2 http://localhost:4180/mcp  # → SSE stream

# 4. REST API (direkte, bypasser oauth2-proxy)
curl -sf http://localhost:8000/api/servers | python3 -m json.tool

# Legg til en test-server
curl -sf -X POST http://localhost:8000/api/servers \
  -H "Content-Type: application/json" \
  -d '{"name":"fetch","type":"npm","package":"@modelcontextprotocol/server-fetch","args":[],"env":{}}' \
  | python3 -m json.tool

curl -sf http://localhost:8000/api/tools | python3 -m json.tool

# 5. Komplett MCP-protokolltest
npx @modelcontextprotocol/inspector
# URL: http://localhost:4180/mcp  |  Header: Authorization: Bearer <ADMIN_TOKEN>
```

---

## Coolify-deployment

1. Opprett nytt prosjekt i Coolify → **Add Resource → Docker Compose**
2. Koble til GitHub-repoet
3. **Compose file path:** `docker-compose.yml`
4. Legg inn miljøvariabler (alle fra `.env` unntatt `OAUTH2_PROXY_COOKIE_SECURE`)
5. Klikk **Deploy** — Coolify bygger `mcp-aggregator`-imaget og starter begge tjenestene

> **Merk:** Ikke inkluder `docker-compose.override.yml` i Coolify. Den er i `.gitignore` og vil ikke være tilgjengelig for Coolify.

---

## Konfigurere MCP-klienter

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

## Administrasjon

### Web UI

Gå til `https://<MCP_DOMAIN>/admin` — logger inn med GitHub. Her kan du:

- **MCP Servere** — legge til, aktivere/deaktivere, restarte og slette servere
- **Logger** — se aggregator-logger og child-prosessers stderr i sanntid (Live SSE-stream)
- **Test verktøy** — velg en kjørende server og et verktøy, fyll inn JSON-argumenter og kall det direkte

### REST API

```bash
BASE=http://localhost:8000   # eller https://<domene> via oauth2-proxy

# List servere
curl $BASE/api/servers | jq

# Legg til server
curl -X POST $BASE/api/servers \
  -H "Content-Type: application/json" \
  -d '{"name":"<navn>","type":"pypi|npm|git|cmd","package":"<pakke>","args":[],"env":{}}'

# Aktiver / deaktiver
curl -X POST $BASE/api/servers/<id>/enable
curl -X POST $BASE/api/servers/<id>/disable

# Restart
curl -X POST $BASE/api/servers/<id>/restart

# Slett
curl -X DELETE $BASE/api/servers/<id>

# List alle tools (inkl. inputSchema)
curl $BASE/api/tools | jq

# Kall et verktøy
curl -X POST $BASE/api/tools/call \
  -H "Content-Type: application/json" \
  -d '{"server":"<servernavn>","tool":"<toolnavn>","arguments":{"key":"value"}}'

# Logger (siste 200 oppføringer)
curl "$BASE/api/logs" | jq
curl "$BASE/api/logs?server=<servernavn>" | jq

# Child-prosessens stderr
curl "$BASE/api/logs/<servernavn>/stderr" | jq

# SSE live-stream (blokkerer, bruk med curl --no-buffer)
curl --no-buffer "$BASE/api/logs/stream"
curl --no-buffer "$BASE/api/logs/stream?server=<servernavn>"
```

---

## Servertyper og konfigurasjon

Tools fra child-servere namespaces som `<servernavn>__<toolnavn>` for å unngå konflikter.

### `pypi` — Python-pakker via uvx

Brukes for Python MCP-servere. `uvx` isolerer pakken og håndterer dependencies automatisk.

**Enkel PyPI-pakke:**

| Felt | Verdi |
|------|-------|
| Navn | `fetch` |
| Type | `pypi` |
| Pakke | `mcp-server-fetch` |
| Args | – |

```bash
curl -X POST $BASE/api/servers -H "Content-Type: application/json" \
  -d '{"name":"fetch","type":"pypi","package":"mcp-server-fetch"}'
```

**PyPI-pakke der konsollscript-navn avviker fra pakkenavn** (args[0] = entrypoint):

| Felt | Verdi |
|------|-------|
| Pakke | `markitdown-mcp` |
| Args | `markitdown-mcp` |

Kjører: `uvx --from markitdown-mcp markitdown-mcp`

**Git-URL (enkelt repo):**

| Felt | Verdi |
|------|-------|
| Pakke | `git+https://github.com/org/repo` |
| Args | `entrypoint-navn` |

Kjører: `uvx --from git+https://github.com/org/repo entrypoint-navn`

**Git-URL, monorepo med subpakke (`#subdirectory=`):**

| Felt | Verdi |
|------|-------|
| Pakke | `git+https://github.com/org/repo#subdirectory=packages/my-server` |
| Args | `my-server` |

Kjører: `uvx --from git+https://...#subdirectory=packages/my-server my-server`

**Privat repo** — token i pakke-URL:

| Felt | Verdi |
|------|-------|
| Pakke | `git+https://<token>@github.com/org/repo#subdirectory=packages/my-server` |

Eller som env-variabel:

```bash
-d '{
  "name":"my-server","type":"pypi",
  "package":"git+https://github.com/org/repo#subdirectory=packages/my-server",
  "args":["my-server"],
  "env":{"GIT_ASKPASS":"echo","GITHUB_TOKEN":"<token>"}
}'
```

---

### `npm` — Node.js/TypeScript-pakker via npx

Brukes for Node.js og TypeScript MCP-servere. `npx --yes` laster ned og kjører pakken direkte.

**Publisert npm-pakke:**

| Felt | Verdi |
|------|-------|
| Navn | `filesystem` |
| Type | `npm` |
| Pakke | `@modelcontextprotocol/server-filesystem` |
| Args | `/tillatt/mappe` |

```bash
curl -X POST $BASE/api/servers -H "Content-Type: application/json" \
  -d '{"name":"filesystem","type":"npm","package":"@modelcontextprotocol/server-filesystem","args":["/data"]}'
```

**TypeScript-repo fra GitHub (ikke publisert til npm):**

| Felt | Verdi |
|------|-------|
| Pakke | `git+https://github.com/org/ts-mcp-server` |
| Args | – |

Kjører: `npx --yes git+https://github.com/org/ts-mcp-server`

npm kloner repoet, kjører `npm install` og `prepare`-scriptet (TypeScript-kompilering), og starter binæren fra `bin`-feltet i `package.json`. Forutsetter at pakken har korrekt `prepare`-script og `bin`-oppføring.

**GitHub-kortform og andre npm-støttede URL-formater:**

```
git+https://github.com/org/repo          # full HTTPS
git+ssh://git@github.com/org/repo        # SSH
github:org/repo                          # GitHub-kortform
```

**Privat TypeScript-repo:**

| Felt | Verdi |
|------|-------|
| Pakke | `git+https://<token>@github.com/org/ts-mcp-server` |

---

### `git` — klon og kjør lokalt

Kloner hele repoet til `/data/packages/<navn>` og kjører det derfra. Brukes primært for Python-repoer uten PyPI-publisering.

- **Python-repo** (`pyproject.toml` / `setup.py`): kjøres via `uvx --from <klon-mappe>`
- **Node.js-repo** (`package.json`): kjøres via `node <main>` etter automatisk `npm install` og `npm run build`

| Felt | Verdi |
|------|-------|
| Navn | `my-server` |
| Type | `git` |
| Pakke | `https://github.com/org/repo` |
| Args | evt. entrypoint (Python) |

```bash
curl -X POST $BASE/api/servers -H "Content-Type: application/json" \
  -d '{"name":"my-server","type":"git","package":"https://github.com/org/repo"}'
```

> **Merk:** For TypeScript-repoer fra GitHub er `npm`-modus med `git+https://`-URL enklere — npm cacher globalt og slipper lokal kloning.

---

### `cmd` — direkte kommando

Kjører en vilkårlig kommando. Nyttig for lokalt installerte servere eller custom scripts.

| Felt | Verdi |
|------|-------|
| Navn | `my-tool` |
| Type | `cmd` |
| Pakke | `/usr/local/bin/my-mcp-server` |
| Args | `--config /data/config.json` |

Pakke-feltet splittes på mellomrom og slås sammen med args: `/usr/local/bin/my-mcp-server --config /data/config.json`

---

### Oversikt

| Type | Installasjon | Beste for |
|------|-------------|-----------|
| `pypi` | `uvx` (isolert) | Python MCP-servere fra PyPI eller git |
| `npm` | `npx` (cachet) | Node.js/TS fra npm eller GitHub |
| `git` | Klon → kjør lokalt | Upubliserte Python-repoer |
| `cmd` | Ingen | Lokalt installerte binærer |

---

## Miljøvariabler

| Variabel | Påkrevd | Beskrivelse |
|----------|---------|-------------|
| `MCP_DOMAIN` | ✅ | Offentlig hostname (uten `https://`) |
| `ADMIN_TOKEN` | ✅ | Bearer-token for MCP-klienter. Generer: `openssl rand -hex 32` |
| `COOKIE_SECRET` | ✅ | Session-nøkkel for oauth2-proxy (24 bytes). Generer: `openssl rand -base64 24 \| tr -d '\n'` |
| `GITHUB_CLIENT_ID` | ✅ | GitHub OAuth App Client ID |
| `GITHUB_CLIENT_SECRET` | ✅ | GitHub OAuth App Client Secret |
| `GITHUB_ALLOWED_USERS` | ✅ | Kommaseparert liste med tillatte GitHub-brukernavn |
| `OAUTH2_PROXY_EMAIL_DOMAINS` | ✅ | Sett til `*` (alle e-postdomener; GitHub-brukernavn styrer tilgang) |
| `LOG_LEVEL` | – | `DEBUG` / `INFO` / `WARNING` / `ERROR` (standard: `INFO`) |
| `OAUTH2_PROXY_COOKIE_SECURE` | – | Sett til `false` for lokal test uten HTTPS |

---

## Prosjektstruktur

```
.
├── docker-compose.yml           Produksjonsoppsett (mcp-aggregator + oauth2-proxy)
├── docker-compose.override.yml  Lokal override med porter (ikke i git)
├── Justfile                     Kommandosnarveier
├── scripts/
│   └── init-env.sh              Genererer .env med autogenererte secrets
├── .env.example                 Mal for miljøvariabler
└── aggregator/
    ├── Dockerfile               Python 3.12 + uv + Node.js LTS + git
    ├── pyproject.toml
    └── src/mcp_aggregator/
        ├── main.py              FastAPI-app, lifespan, ruter
        ├── aggregator.py        MCP SSE-server + tool-aggregering
        ├── child_manager.py     Prosess­livssyklus for child-servere
        ├── installer.py         uvx / npx / git clone + npm build
        ├── database.py          SQLite via aiosqlite
        ├── config.py            Env-var innstillinger
        ├── log_capture.py       In-memory loggbuffer + SSE pub-sub
        ├── ui.py                HTMX + Alpine.js admin UI
        └── api/routers.py       REST API
```

## Just-kommandoer

```
just init-env          Generer .env (secrets autogenerert, resten CHANGE_ME)
just init-env -i       Interaktiv modus – spør om alle verdier
just up                Bygg og start i bakgrunnen
just dev               Start i forgrunnen med live logg
just down              Stopp alle tjenester
just down-volumes      Stopp og slett all data (ber om bekreftelse)
just build             Bygg images uten å starte
just restart           Restart alle tjenester
just restart <navn>    Restart én tjeneste
just logs              Følg alle logger
just logs <navn>       Følg én tjeneste
just ps                Vis containerstatus
just health            Sjekk aggregator-helseendepunkt
```
