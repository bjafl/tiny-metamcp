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

COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .

RUN mkdir -p /data && chmod 777 /data

EXPOSE 8000
VOLUME ["/data"]

CMD ["uvicorn", "mcp_aggregator.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
