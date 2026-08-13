# Yad2 Hebrew Search-Understanding Service

A FastAPI service that converts a Hebrew free-text marketplace query
(`"דירת 3 חדרים בירושלים עד מליון שח"`) into structured, taxonomy-validated
search parameters for one of three Yad2 verticals (נדל״ן / רכב / יד_שנייה).
A rule/dictionary classifier and extractor handle the majority of traffic
at zero marginal cost and sub-150ms latency; a two-tier OpenAI cascade —
cheap model first, escalating only on validation failure, degrading
gracefully rather than failing if that's unavailable — covers the queries
rules genuinely can't resolve. Every field either path can emit comes
straight out of `spec/yad2_search_taxonomy.json`, dynamically, so there is
no code path that can invent a field outside the taxonomy.

## Pipeline

```
POST /parse  { "q": "<Hebrew free text>" }
   │
   ▼
sanitize (emoji/control chars/length)
   │
   ▼
normalize (units, ranges, typo+fuzzy correction)
   │
   ▼
cache lookup ──hit──────────────────────────────────► return  (p95  55ms measured)
   │ miss
   ▼
rule/dictionary classify + extract
   │
   ▼
confidence >= 0.58? ──yes──► validate ─► cache write ─► return  (p95  41ms measured)
   │ no
   ▼
OpenAI Tier 1 (gpt-4.1-nano, cheap)      (avg 2.6s uncontended — see below)
   │
   ├─ success ──────────────────────────────────────► validate ─► cache write ─► return
   │
   └─ validation failure / api_error  (17% of fallback calls, measured)
        │
        ▼
      OpenAI Tier 2 (gpt-4.1-mini, stronger)
        │
        ├─ success ────────────────────────────────► validate ─► cache write ─► return
        │
        └─ validation failure / api_error
             │
             ▼
           degrade to rule path's own result, confidence=0.15, notes flag
```

Confidence is *measured*, not asserted, on every path: rule-path confidence
is taxonomy-term coverage scaled by classification margin; a successful
LLM tier's confidence blends the completion's own value-token logprobs
with an embedding cross-check; only the final degrade uses a fixed
constant, because there's genuinely nothing to measure there. Full
rationale: [`docs/decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md`](docs/decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md).

## Quickstart

```bash
cd api
cp .env.example .env
# Edit .env: set OPENAI_API_KEY for the LLM-fallback tier (optional — the
# service runs and degrades gracefully without one).
cd ..
docker compose up --build
```

```bash
curl -X POST http://localhost:8000/parse \
  -H "Content-Type: application/json" \
  --data-binary '{"q":"טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן"}'
# {"category":"רכב","params":{"יצרן":"טויוטה","דגם":"קורולה",
#  "שנה":{"min":2018,"max":2021},"מחיר":{"max":70000},"צבע":"לבן"},
#  "confidence":0.5952,"notes":[]}

curl http://localhost:8000/health
curl http://localhost:8000/metrics
```

```bash
cd api && uv run pytest                                              # 94 tests, no network, ~1.5s
cd api && uv run python ../scripts/loadtest.py --requests 200 --concurrency 20
```

## The honest finding: the model-path latency target isn't met

Cache/rules p95 passes (≤150ms). The 600ms model-path target does not —
measured p95 well into multi-second territory against the real API, root
caused (not just observed): Structured Outputs strict mode forces every
optional field into the response as an explicit `null`, so a 20-28-field
taxonomy schema costs ~2-3s of real generation time per call regardless of
how few fields the query actually needs. A controlled experiment
confirmed the direction (dropping strict mode cut latency 56%) and why
it's not adopted (validation collapsed to 12% — this nano-tier model
doesn't reliably reproduce correct Hebrew object keys without constrained
decoding). Full diagnosis, every number, and the write-behind architecture
recommended as the real next step: **[`docs/requirements.md`](docs/requirements.md)**.

This is on the front page deliberately — a README that only shows the
numbers that pass isn't a credible one.

## Cost model

Real measured tokens (4 live Tier 1 calls, `gpt-4.1-nano`): **3,323.5 avg
prompt tokens, 191.2 avg completion tokens** per fallback call. At
verified pricing, **$0.000410/request** for a Tier-1-only fallback,
**~$0.000655/request** blended at a conservative 15% Tier-2 rate.

| Scenario | Cache hit | Monthly cost (10M queries) | $/request |
|---|---|---|---|
| Conservative | 20% | $2,096 | $0.00021 |
| Optimistic | 60% | $917 | $0.00009 |

Even the conservative scenario is ~$2,100/month for 10M queries, because
caching and the rule-first classifier mean the LLM only ever touches the
minority of traffic. Levers implemented: full-response + word-level
normalization caching, the rule-first classifier itself, two-tier
escalation. Full breakdown and what's discussed-but-not-implemented (OpenAI
prompt caching, embeddings-vs-rules): [`docs/infrastructure/cost-model.md`](docs/infrastructure/cost-model.md).

## Docs

- [`docs/onboarding.md`](docs/onboarding.md) — mental model, 5-minute run
- [`docs/requirements.md`](docs/requirements.md) — full functional/non-functional requirement tables + the complete latency diagnosis
- [`docs/AGENTS.md`](docs/AGENTS.md) · [`docs/conventions/`](docs/conventions/) — how this repo is built and why
- [`docs/decisions/`](docs/decisions/) — the ADRs behind this design
- [`docs/services/search-api.md`](docs/services/search-api.md) — service reference, including known quirks
- [`docs/infrastructure/`](docs/infrastructure/) — observability, cost model, confidence calibration, latency investigation
- [`docs/examples.md`](docs/examples.md) — 8 worked examples across all 3 verticals
- [`INSTRUCTIONS.md`](INSTRUCTIONS.md) — the original assignment brief, untouched
