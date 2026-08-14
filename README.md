# Yad2 Hebrew Search-Understanding Service

A FastAPI service that converts a Hebrew free-text marketplace query
(`"דירת 3 חדרים בירושלים עד מליון שח"`) into structured, taxonomy-validated
search parameters for one of three Yad2 verticals (נדל״ן / רכב / יד_שנייה).
A rule/dictionary classifier and extractor handle the majority of traffic
at zero marginal cost; a two-tier OpenAI cascade — cheap model first,
escalating only on validation failure, degrading gracefully rather than
failing if that's unavailable — covers what rules genuinely can't resolve,
including a dedicated classify-only call for queries with zero taxonomy
signal at all. Every field either path can emit comes straight out of
`data/taxonomy.json`, dynamically, so no code path can invent a field
outside the taxonomy.

## Pipeline

```
POST /parse  { "q": "<Hebrew free text>" }
   │
   ▼
sanitize (emoji/control chars/length)
   │
   ▼
normalize (units, ranges, currency slang, typo+fuzzy correction)
   │
   ▼
cache lookup ──hit────────────────────────────────────► return
   │ miss
   ▼
identical request already in flight? ──yes──► await it, return
   │ no
   ▼
rule/dictionary classify + extract
   │
   ├─ confidence >= 0.58 ──────────────► validate ─► cache write ─► return
   │
   ├─ 0 < confidence < 0.58 (partial signal, a real hint)
   │        │
   │        ▼
   └─ confidence == 0.0 (zero signal) ─► classify-only LLM call ─► degrade honestly on failure
            │
            ▼
      OpenAI Tier 1 (gpt-4.1-nano, cheap)
        │
        ├─ success ──────────────────────────────────► validate ─► cache write ─► return
        │
        └─ validation failure / api_error
             │
             ▼
           OpenAI Tier 2 (gpt-4.1-mini, stronger)
             │
             ├─ success ────────────────────────────► validate ─► cache write ─► return
             │
             └─ validation failure / api_error
                  │
                  ▼
                degrade to rule path's own result, confidence=0.15, notes flag
```

Confidence is measured, not asserted, on every path — see
[`docs/DESIGN.md`](docs/DESIGN.md) for the full formulas, the zero-signal
classification fix, and known disclosed limitations.

## Quickstart

```bash
cd y2_ai_search_api
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
cd y2_ai_search_api && uv run pytest                                             # no network required
cd y2_ai_search_api && uv run python ../scripts/loadtest.py --requests 200 --concurrency 20
```

## Latency

| Path | Measured |
|---|---|
| Cache hit | p95 ~55ms |
| Rules | p95 ~41ms |
| LLM fallback (Tier 1, uncontended) | avg ~2.6s — misses the 600ms target |
| Zero-signal classify + Tier 1 + confidence cross-check | ~6.0s (live example, `docs/examples.md` #9) |

Root cause of the 600ms miss: Structured Outputs strict mode forces every
optional field in a 20-28-field taxonomy schema into the response as an
explicit `null`, regardless of how few fields the query needs — confirmed
by a controlled experiment (dropping strict mode cut latency 56% but
collapsed Hebrew-key validity from 100% to 12%, so it's rejected on
correctness grounds). Full diagnosis and every number: [`docs/DESIGN.md`](docs/DESIGN.md).

## Cost model

Real measured tokens (Tier 1, `gpt-4.1-nano`): 3,323.5 avg prompt tokens,
191.2 avg completion tokens per fallback call. **$0.000410/request**
Tier-1-only, **~$0.000655/request** blended at a conservative 15% Tier-2
rate. Even a conservative 20%-cache-hit scenario projects to ~$2,100/month
at 10M queries. Full breakdown: [`docs/DESIGN.md`](docs/DESIGN.md).

## Docs

- [`docs/DESIGN.md`](docs/DESIGN.md) — pipeline rationale, the zero-signal
  classification fix, confidence methodology, cost model, latency
  diagnosis, known disclosed limitations, future directions
- [`docs/examples.md`](docs/examples.md) — 9 worked examples across all 3
  verticals, including a live end-to-end run of the zero-signal fix
- [`INSTRUCTIONS.md`](INSTRUCTIONS.md) — the original assignment brief,
  untouched
