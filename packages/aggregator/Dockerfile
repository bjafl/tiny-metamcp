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
