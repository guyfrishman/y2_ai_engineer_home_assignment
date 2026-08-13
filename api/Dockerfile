# Multi-stage build using uv for fast, reproducible dependency installs.
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
COPY . .

EXPOSE 8000
# Plain, single-worker Uvicorn — prometheus_client's default registry is
# per-process, so multiple workers would each expose only a partial view
# of /metrics. Scale via container replicas, not --workers. See
# docs/services/search-api.md's Quirks section.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
