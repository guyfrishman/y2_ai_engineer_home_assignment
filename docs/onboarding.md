# Onboarding

Welcome. This is a Hebrew free-text search-understanding service for Yad2:
it turns a query like `דירת 3 חדרים בירושלים עד מליון שח` into structured,
taxonomy-validated search parameters. The goal of this page is to get you
productive in under an hour.

## Day-1 reading list

1. The top-level [`README.md`](../README.md) — what the project is and how to run it.
2. This file.
3. [`conventions/`](conventions/) — read all of them; they're short.
4. [`services/search-api.md`](services/search-api.md) — the one service this repo runs.
5. Skim [`decisions/`](decisions/) to understand *why* the design is the way it is.

## Get it running (5 minutes)

```bash
cd api
cp .env.example .env
uv sync
uv run uvicorn main:app --reload      # http://localhost:8000/docs
```

No API key is required to run locally — auth is a deliberate no-op until
`API_ACCESS_KEY` is set (see [`conventions/configuration.md`](conventions/configuration.md)).
No `OPENAI_API_KEY` is required either: without one, the LLM-fallback tier
short-circuits to a graceful degrade rather than failing the request — see
[ADR 0001](decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md).

Or via Docker:
```bash
docker compose up --build
curl -X POST http://localhost:8000/parse -H "Content-Type: application/json" \
  --data-binary '{"q":"טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן"}'
```

## Mental model

```
POST /parse  { "q": "<Hebrew free text>" }
   │
   ▼
sanitize -> normalize -> cache lookup --hit--> return (<=150ms)
                              │ miss
                              ▼
                 rule/dictionary classify + extract
                              │
        confidence >= threshold? --yes--> validate -> cache write -> return
                              │ no
                              ▼
                 OpenAI Tier 1 (cheap) -> Tier 2 (stronger) -> degrade
                              │
                 validate -> cache write -> return (<=600ms)
```

- The taxonomy (`spec/yad2_search_taxonomy.json`, vendored into
  `api/app/data/taxonomy.json`) is the **single source of truth** for which
  fields exist. `taxonomy_models.py` builds Pydantic models from it at
  import time — there is no hand-maintained field list to fall out of sync.
- The rule path and the LLM fallback path validate against the **exact same**
  Pydantic models, so "strict schema, allowlisted fields" holds identically
  on both paths.
- Everything talks to OpenAI through `OpenAIRepository`, and only the
  LLM-fallback branch of the pipeline does — the rule/cache path never makes
  a network call.

## The layout

```
api/app/
├── config.py            # typed settings (env-driven)
├── logger.py             # @log_activity + session/trace ContextVars
├── security.py           # X-API-Key dependency
├── metrics.py             # custom Prometheus counters/histograms
├── data/taxonomy.json     # vendored copy of spec/yad2_search_taxonomy.json
├── prompts/                # fixed LLM system prompts
├── routers/                # thin HTTP handlers (search.py, ping.py), composed in api.py
├── services/                # orchestration: sanitize, normalize, classify,
│                             #   extract, LLM confidence, LLM fallback, parse
├── repositories/             # taxonomy, cache, OpenAI client — behind interfaces
└── schema/                    # pydantic request/response + taxonomy models

scripts/
└── loadtest.py            # asyncio+httpx concurrent load test, PASS/FAIL vs SLA
```

## How work gets done here

See [`conventions/work-protocol.md`](conventions/work-protocol.md). In short:
plan first, work one verifiable step at a time, and don't move on until the
current step actually runs.
