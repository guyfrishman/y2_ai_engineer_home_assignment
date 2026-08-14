# search-api — Hebrew free-text search-understanding service

**Path:** `y2_ai_search_api/` · **Package/Port:** `yad2-search-api` · `8000` · **Status:** ✅ implemented

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
y2_ai_search_api/
├── main.py                  # mounts routers, wires Instrumentator
├── config.py                 # Settings
├── logger.py                  # log_event, trace_id ContextVar
├── security.py                 # X-API-Key dependency
├── metrics.py                   # custom Prometheus counters/histograms
├── data/taxonomy.json            # vendored copy of spec/yad2_search_taxonomy.json
├── prompts/system_prompts.py      # fixed Tier 1/Tier 2 extraction prompts
├── repositories/
│   ├── taxonomy_repository.py  # loads + indexes taxonomy once
│   ├── cache_repository.py      # CacheRepository(ABC) + InMemoryTTLCache
│   └── openai_repository.py      # AsyncOpenAI-backed client
├── schema/
│   ├── taxonomy_models.py    # dynamically-built per-vertical Pydantic models
│   ├── requests.py            # ParseRequest
│   └── responses.py            # ParseResponse, HealthResponse
├── services/
│   ├── sanitizer_service.py     # strip control chars/emoji, cap length
│   ├── normalizer_service.py     # units, ranges, typo correction
│   ├── classifier_service.py      # rule-based vertical detection + confidence
│   ├── extractor_service.py        # dict-driven, per-vertical field extraction
│   ├── llm_confidence_service.py    # logprob + embedding blended confidence
│   ├── llm_fallback_service.py       # two-tier LLM cascade
│   └── parse_service.py               # orchestrates the full pipeline
├── routers/
│   └── api.py, search.py, ping.py
└── tests/                     # 99 tests, no network — see docs/conventions/testing.md
```

Follows the repo conventions — [routers](../conventions/routers.md),
[repositories](../conventions/repositories.md), [logging](../conventions/logging.md),
[config](../conventions/configuration.md), [llm-usage](../conventions/llm-usage.md).

## Run

```bash
cd y2_ai_search_api
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
  behind a load balancer, not via `--workers` — validated end-to-end (3
  replicas behind nginx, temporary validation infrastructure, since torn
  down) in `docs/infrastructure/observability.md`'s multi-replica section
  and `docs/infrastructure/latency-investigation.md`. The `Dockerfile`'s
  `CMD` has no worker count and adds only connection-handling flags
  (`--backlog`, `--limit-concurrency`, `--timeout-keep-alive` — see that
  doc for what they did and didn't change).
- **`asyncio.to_thread`/thread pools were deliberately *not* used for the
  rule/cache path.** That work is CPU-bound Python (regex scanning, Pydantic
  validation) — the GIL means threading it adds scheduling overhead with no
  real parallelism. Only the LLM-fallback branch uses `async`/`await`
  (via `AsyncOpenAI`), because that's the one branch with real blocking
  network I/O. Measured under load: threading the whole pipeline actually
  *increased* p95 latency versus this split approach — see
  `scripts/loadtest.py`'s output history in the root README.
- **The taxonomy file is vendored, not read from `spec/` at runtime.**
  `y2_ai_search_api/data/taxonomy.json` is a copy of `spec/yad2_search_taxonomy.json`,
  kept in sync manually. This keeps the Docker build self-contained
  (`y2_ai_search_api/` doesn't need the repo root in its build context) at the cost of a
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
  across several, despite doing no network I/O). Seven candidate causes
  have now been tested directly, not assumed, across two investigation
  rounds, and all seven are ruled out: `@log_activity`, the classifier's
  taxonomy-term scan, raw CPU/memory saturation, uvicorn's
  backlog/keep-alive tuning, the AsyncOpenAI client's connection-pool size,
  and — the biggest lever tried — running 3x the capacity behind a real
  load balancer. None moved cache/rules p95 outside its existing
  run-to-run noise band. What *is* confirmed causally (a direct A/B test:
  0/144 vs. 26/129 requests over 100ms at the same concurrency) is that the
  stall only appears when concurrent LLM-path traffic is present, and a
  bimodal latency distribution (most requests fast, a tight cluster stuck
  at a near-identical delay) points at a shared blocking resource, not a
  per-request cost. Full methodology and every number:
  `docs/infrastructure/latency-investigation.md`'s "Docker/infra
  investigation" section. Leading remaining hypothesis, not yet isolated:
  infrastructure shared across every instance alike — most concretely, the
  Docker Desktop WSL2 network translation layer between this project's test
  client and the containers in this development environment.

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
