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
| `POST` | `/parse` | `X-API-Key` (no-op if `API_ACCESS_KEY` unset) | Parse a Hebrew query. Body: `{"q": "<text>"}`. Returns `{category, params, confidence, notes}`. Sets an `X-Parse-Path` response header (`cache`/`rules`/`llm`/`coalesced` — the last when an identical request was already in flight, see `docs/conventions/repositories.md`) — observability only, not part of the JSON contract. |
| `GET` | `/health` | open | Liveness probe; also reports `taxonomy_version`. |
| `GET` | `/metrics` | open | Prometheus exposition — HTTP-level (via `prometheus-fastapi-instrumentator`) plus custom pipeline counters/histograms (see `docs/infrastructure/observability.md`). |

## Layout

```
api/
├── main.py                  # mounts routers, wires Instrumentator
├── app/
│   ├── config.py             # Settings
│   ├── logger.py              # log_event, trace_id ContextVar
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
└── tests/                     # 99 tests, no network — see docs/conventions/testing.md
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
  `logprobs` (a smaller, ~500ms contributor) and not base network/model
  latency.

  Per-phase breakdown, from 12 sequential (uncontended) real fallback
  calls: Tier 1 alone averages **2,613ms** (already ~4x budget with zero
  concurrency involved); Tier 2 escalation fires on **17%** of fallback
  calls (2/12 in this sample), and an escalated request's total latency
  (Tier 1 attempt + Tier 2) averages **4,901ms** — ≈1.9x a non-escalated
  request, roughly the "doubles" a two-step cascade implies; the
  confidence-calc step (value-token logprob math, which is local/cheap,
  plus 2 embedding API calls) adds a further **~173ms average** — real,
  but secondary next to the schema cost.

  Schema-reuse was checked directly, not assumed: `_strict_json_schema`'s
  output was verified byte-identical across repeated rebuilds for the same
  model class, but was previously being reconstructed from scratch on
  every call rather than cached — a real, fixable inefficiency, now fixed
  via `functools.lru_cache`. Since the wire content was already stable
  before that fix, memoizing it didn't change measured latency, which
  confirms the bottleneck is on OpenAI's serving side for a schema this
  size, not a caching bug in this codebase.

  Under concurrent load (a loadtest run with ~27 real fallback calls in
  flight) model-path p95 climbed further — consistent with
  queuing/throttling under burst concurrent traffic from one API key — and
  the cache/rules path's own p95 also degraded in the same runs (200-700ms
  across several, despite doing no network I/O). Two candidate causes were
  tested directly, not assumed. `@log_activity` (a `json.dumps` + recursive
  truncation on every function call) was removed entirely as part of the
  logging rewrite (`docs/conventions/logging.md`), and the same loadtest
  re-run at the same concurrency showed **no improvement** — cache/rules
  p95 was still 200-700ms. `classifier_service._scan_term_occurrences`
  (O(number of taxonomy terms) — 241 regex patterns checked per query) was
  profiled in isolation: ~0.11ms per call, ~0.14ms for the full
  sanitize→normalize→classify→extract pipeline; even 20 requests
  serializing entirely behind each other on the GIL is only ~2.8ms of
  aggregate queueing — three orders of magnitude short of what's observed,
  so no rewrite was made. Across three loadtest runs total, cache-only
  traffic (no concurrent LLM calls) was consistently fast (~50ms); any run
  with real concurrent LLM traffic showed the degradation regardless of
  either candidate. That rules out both as the cause and points toward
  infra-level contention (client connection pooling, or Docker Desktop's
  networking layer under sustained external call volume) instead — not
  isolated further than that, but two "it's this code" explanations are
  now tested and rejected, not live ones.

  The credible paths to closing the core gap, not yet implemented: (1)
  scope the `יד_שנייה` schema to just the rule path's candidate
  subcategory instead of unioning all subcategories' fields — untested
  whether this helps meaningfully, since latency looked roughly flat
  across this project's 20–28-field range; (2) drop strict-mode Structured
  Outputs in favor of a looser prompt + post-hoc Pydantic validation
  (trades schema-enforced generation for speed — validation still catches
  a bad shape, just without generation-time constraint); (3) a
  faster/smaller model, if one is found to support both Structured Outputs
  and `logprobs` reliably at lower latency. See the root README's
  Non-Functional Requirements table for the measured numbers this claim is
  based on.
