# Yad2 Hebrew Search-Understanding Service

A FastAPI service that converts a Hebrew free-text marketplace query
(`"דירת 3 חדרים בירושלים עד מליון שח"`) into structured, taxonomy-validated
search parameters for one of three Yad2 verticals (נדל״ן / רכב / יד_שנייה).
It runs a hybrid pipeline: a rule/dictionary classifier and extractor
handle the majority of traffic at zero marginal cost and sub-150ms
latency, falling back to a two-tier OpenAI cascade — cheap model first,
escalating only on validation failure, degrading gracefully rather than
failing if that's unavailable — for the queries rules genuinely can't
resolve. Every field either path can emit is dynamically generated from
`spec/yad2_search_taxonomy.json`, so there is no code path that can invent
a field outside the taxonomy.

**`INSTRUCTIONS.md`** holds the original assignment brief, untouched.
**`docs/`** holds the full design rationale, conventions, and ADRs this
README summarizes — start at [`docs/onboarding.md`](docs/onboarding.md) if
you want the deeper mental model.

---

## Functional Requirements

| Requirement | Implementation | Verified by |
|---|---|---|
| `POST /parse`: Hebrew text → `{category, params, confidence, notes}` | `api/app/routers/search.py`, `api/app/services/parse_service.py` | `api/tests/test_parse_api.py`; live `curl` — see Quickstart |
| Detect vertical (נדל״ן / רכב / יד_שנייה) | `api/app/services/classifier_service.py` — taxonomy-term coverage scoring | `api/tests/test_classifier_service.py` |
| Extract + normalize fields per taxonomy only | `api/app/services/extractor_service.py` + `api/app/schema/taxonomy_models.py` (dynamically built, `extra="forbid"`, `Literal`-typed enums, cross-field sector/subcategory check) | `api/tests/test_extractor_service.py`, `api/tests/test_security_redteam.py` |
| Reject/flag unknown fields, never invent keys | Same taxonomy models — structurally impossible to emit an unlisted field | `test_pydantic_model_rejects_a_directly_injected_unknown_field`, `test_extracted_params_never_include_a_field_outside_the_taxonomy` |
| `GET /health` | `api/app/routers/ping.py` — also reports `taxonomy_version` | `test_health_is_open_and_reports_taxonomy_version` |
| `GET /metrics` | `api/app/metrics.py` + `prometheus-fastapi-instrumentator`, wired in `api/main.py` | `test_metrics_endpoint_is_open_and_exposes_custom_counters`; live `curl` |
| Typo/slang tolerance | `api/app/services/normalizer_service.py` — static typo map + `rapidfuzz` fuzzy fallback + preposition-prefix stripping | `api/tests/test_normalizer_service.py`; slang cases in `test_security_redteam.py` |
| 5-10 worked examples with expected JSON | [`docs/examples.md`](docs/examples.md) — 8 examples, all asserted in `api/tests/test_extractor_service.py` | Re-run: `uv run pytest tests/test_extractor_service.py -v` |

## Non-Functional Requirements

| Requirement | Implementation | Verified by (real measurement) |
|---|---|---|
| Latency: cache/rules p95 ≤150ms | In-process cache + pure-function rule pipeline, no I/O | **PASS** — p95 55.2ms (cache), 40.6ms (rules); see `scripts/loadtest.py` run below |
| Latency: model path p95 ≤600ms | Two-tier OpenAI cascade, `api/app/services/llm_fallback_service.py` | **FAIL** — p95 6,660ms measured against the real API. Root-caused, not hidden: see [the honest writeup below](#a-real-non-functional-requirement-this-service-does-not-meet) |
| Throughput ≥12 QPS/instance | Async end-to-end on the one real-I/O branch (`AsyncOpenAI`); sync CPU-bound rule path stays un-threaded (GIL — threading it measurably *increased* p95, see `docs/services/search-api.md`'s Quirks) | **PASS** — 29.76 QPS measured with real LLM traffic in the mix, 533+ QPS on a rules/cache-heavy mix |
| Caching (query, normalization) | Full-response cache (`api/app/repositories/cache_repository.py`, `cachetools.TTLCache`, taxonomy-version-keyed) **and** a separate word-level `functools.lru_cache` on typo correction (`normalizer_service.py`) | `api/tests/test_cache_repository.py`; `test_correct_word_is_memoized` |
| Cost tracking, $/request, 10M/mo estimate | `api/app/metrics.py` (`parse_tokens_total`, `parse_cost_usd_total`) computed from a verified pricing table | [`docs/infrastructure/cost-model.md`](docs/infrastructure/cost-model.md) — real measured tokens, see summary below |
| Observability: requests/category, error rate, p50/p95, cache hit ratio, tokens, cost, model success/failure | `api/app/metrics.py` — 6 custom Prometheus metrics + HTTP-level instrumentation | [`docs/infrastructure/observability.md`](docs/infrastructure/observability.md); live `GET /metrics` |
| Structured logs: parsing decisions & security events | `api/app/logger.py`'s `log_metric` — security events use a distinct `security_`-prefixed `event=` tag, greppable separately | [`docs/conventions/logging.md`](docs/conventions/logging.md) |
| Fixed system prompts, allowlisted fields | `api/app/prompts/system_prompts.py` — never interpolates user input beyond the vertical name | [`docs/conventions/llm-usage.md`](docs/conventions/llm-usage.md) |
| Strict JSON Schema validation | Taxonomy Pydantic models, `extra="forbid"`, `Literal` enums, cross-field validator; same models used for both the rule path and as the LLM's Structured Outputs schema | `api/tests/test_llm_fallback_service.py::test_strict_json_schema_has_no_optional_fields_and_forbids_extras` |
| Input sanitization (emoji/control chars/length) | `api/app/services/sanitizer_service.py` | `api/tests/test_sanitizer_service.py` |
| Red-team tests (injection, unicode, oversized, slang) | `api/tests/test_security_redteam.py` — 23 tests | `uv run pytest tests/test_security_redteam.py -v` — all pass |

### A real non-functional requirement this service does not meet

The LLM-fallback path's p95 is **6.7 seconds against a 600ms target** —
measured against the real OpenAI API, not simulated. Root-caused by
isolating each variable independently:

| Configuration | Measured latency |
|---|---|
| Plain chat completion, no schema | ~500-850ms |
| + Structured Outputs strict-mode schema (this service's taxonomy models) | **~2,000-3,000ms** |
| + `logprobs=True` on top of the schema | +~500ms |

The JSON-schema-constrained decoding itself — not `logprobs`, not base
model/network latency — is the dominant cost, and it was roughly flat
across this project's three schemas (20–28 fields each), not clearly
correlated with field count in that range. This is a genuine, unresolved
gap. The credible paths to closing it, honestly not yet implemented:
scoping the `יד_שנייה` schema to the rule path's candidate subcategory
instead of unioning all subcategories' fields; dropping strict-mode
Structured Outputs for a looser prompt with post-hoc Pydantic validation;
or a model confirmed faster at this schema complexity. Full detail:
[`docs/services/search-api.md`](docs/services/search-api.md)'s Quirks
section.

This is disclosed here deliberately, not in fine print — a README that
only shows the numbers that pass isn't a credible README.

---

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
OpenAI Tier 1 (gpt-4.1-nano, cheap)
   │
   ├─ success ──────────────────────────────────────► validate ─► cache write ─► return
   │
   └─ validation failure / api_error
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
           (p95 6,660ms measured when a real model call is attempted — see above)
```

Confidence is *measured*, not asserted, on every path: rule-path confidence
is taxonomy-term coverage scaled by classification margin; a successful
LLM tier's confidence blends the completion's own value-token logprobs
with an embedding cross-check; only the final degrade uses a fixed
constant, because there's genuinely nothing to measure there. Full
rationale: [`docs/decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md`](docs/decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md).

---

## Cost model

Real measured tokens (4 live Tier 1 calls, `gpt-4.1-nano`): **3,323.5 avg
prompt tokens, 191.2 avg completion tokens** per fallback call, plus ~49
embedding tokens for the confidence cross-check. At verified pricing
($0.10/$0.40 per 1M tokens):

**$0.000410/request** for a Tier-1-only fallback; **~$0.000655/request**
blended assuming a conservative 15% Tier-2-escalation rate.

| Scenario | Cache hit | Monthly cost (10M queries) | $/request |
|---|---|---|---|
| Conservative | 20% | $2,096 | $0.00021 |
| Moderate | 50% | $1,310 | $0.00013 |
| Optimistic | 60% | $917 | $0.00009 |

**Even the conservative scenario is ~$2,100/month for 10M queries** —
because caching and the rule-first classifier mean the LLM only ever
touches the minority of traffic. Levers actually implemented: full-response
caching, word-level normalization caching, the rule-first classifier
itself, and two-tier escalation (cheapest suitable model first). Falls out
of the design for free: OpenAI's automatic prompt-caching discount on the
repeated fixed system prompt (not yet observed in this project's own
measurements — `cached_tokens: 0` — the baseline above is conservative and
doesn't assume it). Deliberately not used on the main path: embeddings —
rules are cheaper, deterministic, and the only way to hit the 150ms
cache/rules SLA; embeddings are used narrowly for the LLM-tier confidence
cross-check only. Full breakdown, formulas, and caveats:
[`docs/infrastructure/cost-model.md`](docs/infrastructure/cost-model.md).

---

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

Run the test suite (90 tests, no network, ~1.5s):
```bash
cd api && uv run pytest
```

Run the load test against a running instance:
```bash
cd api && uv run python ../scripts/loadtest.py --requests 200 --concurrency 20
```

## More

- [`docs/onboarding.md`](docs/onboarding.md) — mental model, 5-minute run
- [`docs/AGENTS.md`](docs/AGENTS.md) — how to work in this repo
- [`docs/conventions/`](docs/conventions/) — routers, repositories, logging, config, LLM usage, testing, code style, work protocol
- [`docs/decisions/`](docs/decisions/) — the two ADRs behind this design
- [`docs/services/search-api.md`](docs/services/search-api.md) — service reference, including known quirks
- [`docs/infrastructure/`](docs/infrastructure/) — observability, cost model, confidence calibration
- [`docs/examples.md`](docs/examples.md) — 8 worked examples across all 3 verticals
- [`INSTRUCTIONS.md`](INSTRUCTIONS.md) — the original assignment brief
