# Requirements — full detail

The root README summarizes this; this page is the complete
requirement-by-requirement record, per the assignment brief
(`spec/assignment.md`).

## Functional Requirements

| Requirement | Implementation | Verified by |
|---|---|---|
| `POST /parse`: Hebrew text → `{category, params, confidence, notes}` | `api/app/routers/search.py`, `api/app/services/parse_service.py` | `api/tests/test_parse_api.py`; live `curl` — see root README Quickstart |
| Detect vertical (נדל״ן / רכב / יד_שנייה) | `api/app/services/classifier_service.py` — taxonomy-term coverage scoring | `api/tests/test_classifier_service.py` |
| Extract + normalize fields per taxonomy only | `api/app/services/extractor_service.py` + `api/app/schema/taxonomy_models.py` (dynamically built, `extra="forbid"`, `Literal`-typed enums, cross-field sector/subcategory check) | `api/tests/test_extractor_service.py`, `api/tests/test_security_redteam.py` |
| Reject/flag unknown fields, never invent keys | Same taxonomy models — structurally impossible to emit an unlisted field | `test_pydantic_model_rejects_a_directly_injected_unknown_field`, `test_extracted_params_never_include_a_field_outside_the_taxonomy` |
| `GET /health` | `api/app/routers/ping.py` — also reports `taxonomy_version` | `test_health_is_open_and_reports_taxonomy_version` |
| `GET /metrics` | `api/app/metrics.py` + `prometheus-fastapi-instrumentator`, wired in `api/main.py` | `test_metrics_endpoint_is_open_and_exposes_custom_counters`; live `curl` |
| Typo/slang tolerance | `api/app/services/normalizer_service.py` — static typo map + `rapidfuzz` fuzzy fallback + preposition-prefix stripping | `api/tests/test_normalizer_service.py`; slang cases in `test_security_redteam.py` |
| 5-10 worked examples with expected JSON | [`examples.md`](examples.md) — 8 examples, all asserted in `api/tests/test_extractor_service.py` | Re-run: `uv run pytest tests/test_extractor_service.py -v` |

## Non-Functional Requirements

| Requirement | Implementation | Verified by (real measurement) |
|---|---|---|
| Latency: cache/rules p95 ≤150ms | In-process cache + pure-function rule pipeline, no I/O | **PASS** at moderate concurrency (p95 55ms cache / 41ms rules); **degrades under heavy concurrent real-LLM traffic** (p95 200-700ms at 20 concurrent, ~27 of them real API calls) — see the latency section below |
| Latency: model path p95 ≤600ms | Two-tier OpenAI cascade, `api/app/services/llm_fallback_service.py` | **FAIL** — see [the full diagnosis below](#the-llm-path-latency-miss-full-diagnosis): ~2.6s avg for an isolated, uncontended Tier 1 call alone, ~4x over budget before any concurrency is involved |
| Throughput ≥12 QPS/instance | Async end-to-end on the one real-I/O branch (`AsyncOpenAI`); sync CPU-bound rule path stays un-threaded (GIL — threading it measurably *increased* p95, see `services/search-api.md`'s Quirks) | **PASS** — 15-30 QPS measured with real LLM traffic in the mix (varies run-to-run against the live API), 533+ QPS on a rules/cache-heavy mix |
| Caching (query, normalization) | Full-response cache (`api/app/repositories/cache_repository.py`, `cachetools.TTLCache`, taxonomy-version-keyed) **and** a separate word-level `functools.lru_cache` on typo correction (`normalizer_service.py`) | `api/tests/test_cache_repository.py`; `test_correct_word_is_memoized` |
| Cost tracking, $/request, 10M/mo estimate | `api/app/metrics.py` (`parse_tokens_total`, `parse_cost_usd_total`) computed from a verified pricing table | [`infrastructure/cost-model.md`](infrastructure/cost-model.md) — real measured tokens |
| Observability: requests/category, error rate, p50/p95, cache hit ratio, tokens, cost, model success/failure | `api/app/metrics.py` — 7 custom Prometheus metrics + HTTP-level instrumentation | [`infrastructure/observability.md`](infrastructure/observability.md); live `GET /metrics` |
| Structured logs: parsing decisions & security events | `api/app/logger.py`'s `log_event`, `trace_id` set once per request by middleware — security events use a distinct `security_`-prefixed `event=` tag, greppable separately | [`conventions/logging.md`](conventions/logging.md) |
| Fixed system prompts, allowlisted fields | `api/app/prompts/system_prompts.py` — never interpolates user input beyond the vertical name | [`conventions/llm-usage.md`](conventions/llm-usage.md) |
| Strict JSON Schema validation | Taxonomy Pydantic models, `extra="forbid"`, `Literal` enums, cross-field validator; same models used for both the rule path and as the LLM's Structured Outputs schema | `api/tests/test_llm_fallback_service.py::test_strict_json_schema_has_no_optional_fields_and_forbids_extras` |
| Input sanitization (emoji/control chars/length) | `api/app/services/sanitizer_service.py` | `api/tests/test_sanitizer_service.py` |
| Red-team tests (injection, unicode, oversized, slang) | `api/tests/test_security_redteam.py` — 23 tests | `uv run pytest tests/test_security_redteam.py -v` — all pass |

## The LLM-path latency miss: full diagnosis

Measured against the real OpenAI API — the finding held up under a second
round of scrutiny (isolating schema-caching, concurrency, and per-tier
contribution as separate variables) and was diagnosed further, not
softened.

**Root cause, isolated by holding every other variable fixed:**

| Configuration | Measured latency |
|---|---|
| Plain chat completion, no schema | ~500-850ms |
| + Structured Outputs strict-mode schema (this service's taxonomy models) | **~2,000-3,000ms** |
| + `logprobs=True` on top of the schema | +~500ms |

The JSON-schema-constrained decoding itself — not `logprobs`, not base
model/network latency — is the dominant cost, and it was roughly flat
across this project's three schemas (20–28 fields each), not clearly
correlated with field count in that range.

**Output-volume-bound, confirmed from real completion-token data**
(`infrastructure/latency-investigation.md`): mean completion tokens across
5 real Tier 1 calls was 183.2, at an implied ~70 tokens/sec — a normal
generation rate, not the throttled rate you'd expect from a heavy
per-token grammar tax. Strict mode requires every property in `required`
(nullable unions), so a 20-28-field schema emits that many key-value pairs
per call — mostly null — regardless of how many the query actually
supports. That's confirmed by a controlled experiment: dropping
`strict=true` (letting the model emit only the fields it found) cut
average completion tokens from 192 to 98 and latency by 56% — but also
collapsed validation from 100% to 12%, because without strict mode's
constrained decoding this nano-tier model doesn't reliably reproduce
correct right-to-left Hebrew object keys at all (real observed failure:
`{"ייחת_היוכנ אפורכן":null,"סוגי רכב":null}` — garbled, not just
incomplete). That variant was rejected — full experiment, all five
variants tested, in `infrastructure/latency-investigation.md`.

**Per-phase breakdown**, from 12 sequential (uncontended — no concurrency,
so this isolates the per-call floor, not a contention artifact) real
fallback calls across all three verticals:

| Phase | Measured |
|---|---|
| Tier 1 call (non-escalated) | avg **2,613ms** (range 1,414–4,359ms) — already ~4x the 600ms budget alone |
| Escalation rate | **17%** (2/12 in this sample) |
| Total latency when escalated (Tier 1 attempt + Tier 2) | avg **4,901ms** — ≈1.9x a non-escalated request, roughly the "doubles" a two-step cascade implies |
| Confidence-calc overhead (value-token logprob math + 2 embedding calls) | avg **173ms** (0–462ms) — real, but secondary next to the schema cost above |

**Schema caching, checked directly — not the bug:** the same schema dict
(byte-identical JSON across rebuilds, verified) was previously being
reconstructed from the Pydantic model on every call rather than reused.
That *was* a real, fixable inefficiency — now memoized with
`functools.lru_cache` in `llm_fallback_service._strict_json_schema` — but
since the wire content was already stable before the fix, this didn't
change measured latency, confirming the bottleneck is genuinely on
OpenAI's serving side for a schema this size, not a caching bug in this
codebase.

**A correctness bug, fixed along the way:** `OpenAIRepository`'s client
was using the SDK's own defaults (`max_retries=2`, `timeout=600s`) — every
latency number up to that point could have silently included up to three
sequential attempts with backoff. Fixed to `max_retries=0`,
`timeout=5.0s`. Re-measuring afterward: average latency didn't drop, but
escalation rate rose sharply (17% → 42% in that comparison run) — evidence
that silent retries had been masking real transient Tier 1 failures (or
that 5s is tight enough to cut off some legitimate slow completions, or
both; not cleanly separated with the sample size used). Full numbers:
`infrastructure/latency-investigation.md`.

**Under concurrent load, it gets worse, and so — surprisingly — does the
cache/rules path.** A loadtest run with ~27 real concurrent fallback calls
in the mix showed model-path p95 climbing well past the uncontended Tier 1
floor above (consistent with queuing/throttling on OpenAI's side under
burst concurrent traffic from one API key), and cache/rules p95 also
degrading to 200-700ms in the same runs, despite that code path doing no
network I/O of its own.

**Seven candidate causes have now been tested directly across two
investigation rounds, and all seven are ruled out.** Round one:
`@log_activity` (a per-function decorator emitting a `json.dumps` +
recursive truncation on every call) was removed entirely (see
`conventions/logging.md`) and re-tested — cache/rules p95 unchanged
(still 200-700ms). The classifier's per-request taxonomy-term scan
(`classifier_service._scan_term_occurrences`, 241 compiled regex patterns
per query) was profiled directly: ~0.11ms per call, ~0.14ms for the full
pipeline, ~2.8ms worst-case aggregate at 20 concurrent requests — three
orders of magnitude short of what's observed. Round two (full methodology
and every number: `infrastructure/latency-investigation.md`'s
"Docker/infra investigation" section) tested infra/networking candidates
specifically: raw CPU/memory saturation (ruled out — max 23-25% of 24
cores via `docker stats`), uvicorn's `--backlog`/`--timeout-keep-alive`
tuning (no reproducible improvement), the AsyncOpenAI client's
connection-pool size (premise refuted — the SDK already configures a far
more generous pool than assumed; no effect), and 3x replica capacity
behind a real nginx load balancer (no reproducible improvement, and a
confirmed real cost: cache-hit rate drops since each replica keeps its
own independent cache).

**What is confirmed, not just ruled out:** a direct A/B test at fixed
concurrency (LLM-path traffic present vs. completely absent) shows the
stall only appears with concurrent LLM traffic — 0/144 vs. 26/129 cache
requests over 100ms — and the slow requests cluster tightly around one
latency value rather than spreading out, the signature of a shared
blocking resource releasing a batch of waiters together, not a
per-request cost. None of the seven tested candidates are that resource.
The leading remaining hypothesis, not yet isolated: infrastructure shared
across every instance alike (all of them, including the 3-replica setup,
still sit behind the same Docker Desktop WSL2 network translation layer
between this project's test client and its containers) — confirming that
specific layer needs a different test environment (bare-Linux Docker, or
a loadtest client co-located inside the Docker network) than was
available for this investigation.

**The credible paths to closing the core gap, honestly not yet
implemented:** scoping the `יד_שנייה` schema to the rule path's candidate
subcategory instead of unioning all subcategories' fields (untested
whether it would help, given latency looked flat across this project's
20–28-field range); dropping strict-mode Structured Outputs for a looser
prompt with post-hoc Pydantic validation (rejected once already above —
would need a stronger model than nano to keep Hebrew-key reliability, at
higher per-token cost); or a **write-behind architecture** that removes
the LLM call from the request's own critical path entirely — documented as
the recommended next step, with its own trade-off made explicit, in
[`decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md`](decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md#future-direction-write-behind--optimistic-degrade-not-implemented).

This is disclosed here, and in the root README, deliberately — not in fine
print. A README that only shows the numbers that pass isn't a credible one.
