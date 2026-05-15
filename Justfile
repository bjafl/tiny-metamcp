set shell := ["bash", "-c"]

# Vis tilgjengelige kommandoer
default:
    @just --list --unsorted

# ── Miljø ─────────────────────────────────────────────────────────────────────

# Opprett .env med autogenererte secrets.
# Bruk 'just init-env -i' for interaktiv modus (spør om domene og GitHub-verdier).
init-env mode="":
    @bash scripts/init-env.sh "{{mode}}"

# ── Docker Compose ────────────────────────────────────────────────────────────

# Bygg og start alle tjenester i bakgrunnen
up:
    docker compose up -d --build

# Start i forgrunnen med live logg-output
dev:
    docker compose up --build

# Stopp alle tjenester
down:
    docker compose down

# Stopp og slett alle volumes (OBS: sletter persistert data)
down-volumes:
    @read -rp "Sletter ALL data i volumes. Fortsett? [y/N] " c && [[ "${c,,}" == "y" ]]
    docker compose down -v

# Bygg images på nytt uten å starte
build:
    docker compose build

# Restart én eller alle tjenester  ('just restart' eller 'just restart mcp-aggregator')
restart service="":
    #!/usr/bin/env bash
    if [ -n "{{service}}" ]; then
        docker compose restart {{service}}
    else
        docker compose restart
    fi

# ── Logging og status ─────────────────────────────────────────────────────────

# Følg logger ('just logs' eller 'just logs mcp-aggregator')
logs service="":
    #!/usr/bin/env bash
    if [ -n "{{service}}" ]; then
        docker compose logs -f {{service}}
    else
        docker compose logs -f
    fi

# Vis status for alle containere
ps:
    docker compose ps

# Sjekk aggregatorens health-endepunkt
health:
    @curl -sf http://localhost:8000/health | python3 -m json.tool 2>/dev/null \
        || echo "Aggregator ikke tilgjengelig på localhost:8000"
