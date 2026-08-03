set shell := ["bash", "-c"]

# List available commands
default:
    @just --list --unsorted

# ── Environment ───────────────────────────────────────────────────────────────

# Generate .env with auto-generated secrets.
# Use 'just init-env -i' for interactive mode (prompts for domain and GitHub values).
init-env mode="":
    @bash scripts/init-env.sh "{{mode}}"

# ── Testing ───────────────────────────────────────────────────────────────────

# Run the aggregator's test suite
test:
    cd packages/aggregator && uv run pytest

# ── Docker Compose ────────────────────────────────────────────────────────────

# Build and start all services in the background
up:
    docker compose up -d --build

# Start in the foreground with live log output
dev:
    docker compose up --build

# Stop all services
down:
    docker compose down

# Stop and delete all volumes (WARNING: destroys persisted data)
down-volumes:
    @read -rp "Deletes ALL data in volumes. Continue? [y/N] " c && [[ "${c,,}" == "y" ]]
    docker compose down -v

# Rebuild images without starting
build:
    docker compose build

# Restart one or all services  ('just restart' or 'just restart mcp-aggregator')
restart service="":
    #!/usr/bin/env bash
    if [ -n "{{service}}" ]; then
        docker compose restart {{service}}
    else
        docker compose restart
    fi

# ── Logging and status ────────────────────────────────────────────────────────

# Follow logs ('just logs' or 'just logs mcp-aggregator')
logs service="":
    #!/usr/bin/env bash
    if [ -n "{{service}}" ]; then
        docker compose logs -f {{service}}
    else
        docker compose logs -f
    fi

# Show status of all containers
ps:
    docker compose ps

# Check the aggregator health endpoint
health:
    @curl -sf http://localhost:8000/health | python3 -m json.tool 2>/dev/null \
        || echo "Aggregator not available on localhost:8000"
