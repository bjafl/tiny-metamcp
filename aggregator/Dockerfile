FROM python:3.12-slim

# System deps: git + curl (for uv installer) + Node.js LTS
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# uv (provides uvx for isolated PyPI tool execution)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

COPY pyproject.toml .
# Install deps first (layer cache)
RUN pip install --no-cache-dir -e ".[dev]" 2>/dev/null || pip install --no-cache-dir -e .

COPY src/ src/

EXPOSE 8000
VOLUME ["/data"]

CMD ["uvicorn", "mcp_aggregator.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
