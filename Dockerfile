# ── Builder ──────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev

# ── Production ───────────────────────────────────────────────────────────────
FROM python:3.12-slim AS production

RUN groupadd -g 1001 app && useradd -u 1001 -g app -s /bin/sh -m app

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
ENV PATH="/app/.venv/bin:$PATH"

# slim base images ship neither curl nor wget, and the health probe needs one
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV MCP_HTTP_PORT=8080 MCP_HTTP_HOST=0.0.0.0
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:8080/health || exit 1
CMD ["python", "-m", "datadog_mcp"]

LABEL org.opencontainers.image.title="datadog-mcp"
LABEL org.opencontainers.image.description="Datadog MCP server for the MSPbots Agent platform"
LABEL org.opencontainers.image.licenses="Apache-2.0"
