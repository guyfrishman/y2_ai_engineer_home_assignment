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
# No curl/wget in python:3.12-slim -- stdlib urllib avoids adding a package
# just for this. /health is unauthenticated (see security.py), so no
# credentials are needed here.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)" || exit 1

# Plain, single-worker Uvicorn — prometheus_client's default registry is
# per-process, so multiple workers would each expose only a partial view
# of /metrics. Scale via container replicas, not --workers.
#
# --backlog/--limit-concurrency/--timeout-keep-alive: starting values, not
# tuned final numbers. --backlog 4096 (2x uvicorn's own default of 2048)
# gives the OS-level TCP accept queue more room to absorb a burst of fast
# requests arriving while slow LLM-fallback requests are held open.
# --limit-concurrency 100 is a generous ceiling (uvicorn's own default is
# unlimited) meant only to bound runaway load with a clean 503 rather than
# an unbounded queue — setting this too low is a real trade-off, not a free
# safety net. --timeout-keep-alive 5 matches uvicorn's own default, made
# explicit.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--backlog", "4096", "--limit-concurrency", "100", "--timeout-keep-alive", "5"]
