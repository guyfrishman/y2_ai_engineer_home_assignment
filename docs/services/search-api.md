# search-api — Hebrew free-text search-understanding service

**Path:** `api/` · **Package/Port:** `yad2-search-api` · `8000` · **Status:** ✅ implemented

## What it does

Converts a Hebrew free-text query (`"דירת 3 חדרים בירושלים עד מליון שח"`)
into structured, taxonomy-validated Yad2 search parameters: which vertical
(`נדל״ן` / `רכב` / `יד_שנייה`), and a `params` object containing only
allowlisted fields for that vertical. This is the only service in this
repo — there's no separate UI or worker.

## Endpoints / interface

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/parse` | `X-API-Key` (no-op if `API_ACCESS_KEY` unset) | Parse a Hebrew query. Body: `{"q": "<text>"}`. Returns `{category, params, confidence, notes}`. Sets an `X-Parse-Path` response header (`cache`/`rules`/`llm`) — observability only, not part of the JSON contract. |
| `GET` | `/health` | open | Liveness probe; also reports `taxonomy_version`. |
| `GET` | `/metrics` | open | Prometheus exposition — HTTP-level (via `prometheus-fastapi-instrumentator`) plus custom pipeline counters/histograms (see `docs/infrastructure/observability.md`). |

## Layout

```
api/
├── main.py                  # mounts routers, wires Instrumentator
├── app/
│   ├── config.py             # Settings
│   ├── logger.py              # @log_activity / log_metric
│   ├── security.py             # X-API-Key dependency
│   ├── metrics.py               # custom Prometheus counters/histograms
│   ├── data/taxonomy.json        # vendored copy of spec/yad2_search_taxonomy.json
│   ├── prompts/system_prompts.py  # fixed Tier 1/Tier 2 extraction prompts
│   ├── repositories/
│   │   ├── taxonomy_repository.py  # loads + indexes taxonomy once
│   │   ├── cache_repository.py      # CacheRepository(ABC) + InMemoryTTLCache
│   │   └── openai_repository.py      # AsyncOpenAI-backed client
│   ├── schema/
│   │   ├── taxonomy_models.py    # dynamically-built per-vertical Pydantic models
│   │   ├── requests.py            # ParseRequest
│   │   └── responses.py            # ParseResponse, HealthResponse
│   ├── services/
│   │   ├── sanitizer_service.py     # strip control chars/emoji, cap length
│   │   ├── normalizer_service.py     # units, ranges, typo correction
│   │   ├── classifier_service.py      # rule-based vertical detection + confidence
│   │   ├── extractor_service.py        # dict-driven, per-vertical field extraction
│   │   ├── llm_confidence_service.py    # logprob + embedding blended confidence
│   │   ├── llm_fallback_service.py       # two-tier LLM cascade
│   │   └── parse_service.py               # orchestrates the full pipeline
│   └── routers/
│       ├── api.py, search.py, ping.py
└── tests/                     # 88 tests, no network — see docs/conventions/testing.md
```

Follows the repo conventions — [routers](../conventions/routers.md),
[repositories](../conventions/repositories.md), [logging](../conventions/logging.md),
[config](../conventions/configuration.md), [llm-usage](../conventions/llm-usage.md).

## Run

```bash
cd api
cp .env.example .env
uv sync
uv run uvicorn main:app --reload
```

Or:
```bash
docker compose up --build
```

## Dependencies

- **OpenAI** — only on the LLM-fallback branch (rare, by design). See
  [ADR 0002](../decisions/0002-openai-specific-repository.md).
- Nothing else external. The cache is in-process; there's no database, no
  message queue, no other service to reach.

## Quirks

- **Single Uvicorn worker per container — `prometheus_client`'s default
  registry is per-process.** Running multiple `--workers` would mean each
  worker exposes only a partial view of `/metrics` (whichever counter
  increments happened to land on that worker), producing misleading
  numbers rather than an error. Scale horizontally via container replicas
  behind a load balancer, not via `--workers`. The `Dockerfile`'s `CMD` is
  deliberately plain `uvicorn main:app` with no worker count.
- **`asyncio.to_thread`/thread pools were deliberately *not* used for the
  rule/cache path.** That work is CPU-bound Python (regex scanning, Pydantic
  validation) — the GIL means threading it adds scheduling overhead with no
  real parallelism. Only the LLM-fallback branch uses `async`/`await`
  (via `AsyncOpenAI`), because that's the one branch with real blocking
  network I/O. Measured under load: threading the whole pipeline actually
  *increased* p95 latency versus this split approach — see
  `scripts/loadtest.py`'s output history in the root README.
- **The taxonomy file is vendored, not read from `spec/` at runtime.**
  `api/app/data/taxonomy.json` is a copy of `spec/yad2_search_taxonomy.json`,
  kept in sync manually. This keeps the Docker build self-contained
  (`api/` doesn't need the repo root in its build context) at the cost of a
  file that must be re-copied if the source taxonomy changes — acceptable
  for this service's scope; a larger service would resolve this with a
  build step or a shared package.
- **The LLM-fallback path does not meet the 600ms p95 target under real
  measurement — this is a known, unresolved gap, not an oversight.**
  Measured against the live API: a bare chat completion (no schema) is
  ~500-850ms; the *same* call with a Structured Outputs strict-mode schema
  (this service's taxonomy models, 20-28 fields) jumps to ~2-3 **seconds**,
  regardless of which vertical's schema is used. Isolated by testing with
  and without the schema, and with and without `logprobs`, independently —
  the JSON-schema constrained decoding itself is the dominant cost, not
  `logprobs` (a smaller, ~500ms contributor) and not base
  network/model latency. The credible paths to closing this gap, not yet
  implemented: (1) scope the `יד_שנייה` schema to just the rule path's
  candidate subcategory instead of unioning all subcategories' fields —
  untested whether this helps meaningfully, since latency looked roughly
  flat across this project's 20–28-field range; (2) drop strict-mode
  Structured Outputs in favor of a looser prompt + post-hoc Pydantic
  validation (trades schema-enforced generation for speed — validation
  still catches a bad shape, just without generation-time constraint);
  (3) a faster/smaller model, if one is found to support both Structured
  Outputs and `logprobs` reliably at lower latency. See the root README's
  Non-Functional Requirements table for the measured numbers this claim is
  based on.
