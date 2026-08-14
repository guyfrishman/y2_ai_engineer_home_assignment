# Design

Pipeline rationale, confidence methodology, cost model, latency, known
limitations, and future directions for the Yad2 Hebrew search-understanding
service, in one place.

## Pipeline

Every query runs through sanitize → normalize → cache lookup → rule-based
classify+extract first, entirely without a model call. Rules are free,
deterministic, and fast; they handle the common case (a clear brand/city/
property-type match) at zero marginal cost. A model call only happens when
the rule path's confidence is below `confidence_threshold` (0.58):

- **Zero signal** (`confidence == 0.0` exactly — no taxonomy-term or
  cue-word match for *any* vertical): a dedicated, single-field
  classify-only call (`services.llm_fallback_service.run_category_classification`)
  picks the vertical first, then the normal extraction cascade runs scoped
  to that vertical. Without this step, the rule path's `max()` tie-break
  silently returns `Vertical`'s first-declared member — this service
  shipped that bug once; see "The zero-signal bug" below.
- **Partial signal** (`0 < confidence < threshold`): the rule path's own
  vertical is a real, if uncertain, hint — it's handed straight into the
  ordinary two-tier extraction cascade, same as always.
- **Extraction cascade** (used by both cases above): a cheap model
  (`gpt-4.1-nano`) first, escalating to a stronger one (`gpt-4.1-mini`)
  only if the cheap model's output fails the taxonomy's own required-field
  validation. If both tiers fail, the pipeline degrades to the rule path's
  own (sub-threshold) result with a fixed low confidence and a notes entry,
  rather than failing the request.

Every field either path can emit comes straight out of `data/taxonomy.json`,
dynamically, via `schema/taxonomy_models.py`'s `extra="forbid"` Pydantic
models — no code path can invent a field outside the taxonomy.

`gpt-4.1-nano`/`gpt-4.1-mini`, not `gpt-5-nano`/`gpt-5-mini`: verified live
against the real API, the entire GPT-5 family rejects `logprobs` requests
(403 "not allowed to request logprobs from this model"), and this
pipeline's LLM-tier confidence score depends on logprobs.

## The zero-signal bug

Root cause, confirmed reproducible: `"ג'יפ קטן עד 20 אש''ח"` (a car query)
scored 0.0 on every vertical — `ג'יפ` wasn't a taxonomy term or cue word,
`אש''ח` wasn't a recognized currency abbreviation — and Python's `max()`
deterministically returned `Vertical.REAL_ESTATE` (declared first in the
enum), which was then handed to the LLM extraction fallback as the *only*
candidate vertical. The model, correctly following its scoped schema,
produced a confident (0.986) real-estate answer for a car query.
`classification.confidence == 0.0` was already the exact, already-computed
signal that this happened; it was computed and discarded one function
later. Three complementary fixes, verified end to end
(`docs/examples.md`'s example 9, a live run against the real API):

1. **The zero-signal classify-only call** (above) — the structural fix.
   Live-verified: a tiny 286-prompt/8-completion-token call correctly
   resolves the query to `רכב`.
2. **Cue words mechanically derived from the taxonomy** (`TaxonomyRepository._build_cue_words`)
   instead of a hand-authored word list — a word not in the taxonomy, and
   not a sub-word of anything in it, isn't a cue word regardless of how
   useful it might seem; it's what the classify-only call exists for
   instead. Four rules: a vertical's own name, general-attribute field-name
   parts, multi-word taxonomy-value parts, and pruning any candidate that's
   cross-vertical-ambiguous or already redundant with an exact taxonomy
   term. This is what recovers e.g. "בית" (house) from the literal value
   "בית פרטי/וילה" without hand-picking it.
3. **Mark-tolerant pattern generation** (`text_normalization.build_mark_tolerant_pattern`),
   not a hand-enumerated list of quote characters — every taxonomy term
   and hardcoded unit/currency abbreviation (`ש״ח`, `כ״ס`, `מ״ר`, ...)
   compiles to a regex where punctuation *within* a token (geresh,
   gershayim, ASCII/curly quotes, slashes) is optional and variant-tolerant,
   because none of those characters are ever the load-bearing part of a
   match — only the alphanumeric skeleton is. Fixes the same failure mode
   for currency abbreviations too: `אש"ח`/`אש''ח`/`אש״ח` all expand to the
   same `"20000 ש״ח"`.

   Tolerating *zero* marks has one sharp edge, found and fixed during live
   verification: a short unit word's tolerant pattern (e.g. `ש״ח`'s
   `"ש"+[marks]*+"ח"`) degenerates to a bare 2-letter substring match with
   no trailing boundary — it matched the first two letters of `שחור`
   ("black"), so `"...S23 ... שחור"` misread the `23` in a phone model name
   as a price. Every unit/currency pattern now requires `(?!\w)` right
   after it (not `(?!\S)`, which would also reject ordinary trailing
   punctuation) — blocks the substring match while still letting a unit
   word sit next to a period or comma. A related, actually pre-existing
   instance of the same root cause (`"שחור"` → `"ש״חורה"`,
   `"משחק"` → `"מש״חק"`, present before this pass, via the original
   pattern's own unbounded literal `"שח"` alternative) is fixed the same
   way. Regression tests: `test_model_number_digits_are_not_misread_as_price`,
   `test_currency_word_does_not_mangle_unrelated_words_it_is_a_prefix_of`.

A resolution the classify-only call produces is cached at the full-query
level like any other response (`repositories/cache_repository.py`) — a
repeated `"ג'יפ..."`-style query never re-pays the LLM cost. A deliberate,
**not implemented**, future direction: mine that cache for words that
recur across many *different* queries resolving to the same vertical, and
grow the taxonomy's own synonym coverage from that evidence, instead of a
developer guessing synonyms up front.

## Known, disclosed limitations

Found while building the taxonomy-driven test suite
(`tests/test_taxonomy_generated_classification.py`) and left as-is, not
patched around, because fixing them would mean re-introducing exactly the
hand-guessed vocabulary this pass removed:

- **Taxonomy-inherent cross-vertical words are real ties, not gaps.**
  `"מסחרי"` ("commercial") is literally both a vehicles `סוגי_רכב` value
  and a real-estate `מצבי_עסקה` value — a query containing only that word
  scores a genuine 1-1 tie, broken toward `Vertical.REAL_ESTATE` (declared
  first), with real but modest confidence (`test_vehicle_type_words_shared_with_other_verticals_are_a_real_tie`).
  Disambiguates correctly the moment there's a second signal either way.
- **A pre-existing normalizer quirk**: `"למכירה"` ("for sale") isn't
  taxonomy vocabulary, but it's a close enough fuzzy match (one extra
  leading letter) to real estate's own `"מכירה"` term to clear the fuzzy
  correction threshold — an existing used-goods "X למכירה" query picks up
  an unearned real-estate-leaning signal (`test_lamechira_fuzzy_corrects_toward_real_estate`).
- **No stemming, so plural/construct-state forms can attribute to the
  "wrong" vertical.** `"חדר"` (room, singular) ends up a used-goods cue
  word (from the subcategory name `"חדר_שינה"`/bedroom-furniture) rather
  than real estate, because real estate's own field only has the plural
  `"חדרים"`. `"שנת"` (construct-state "year of", from `"שנת_ייצור"`) ends
  up used-goods-only, competing with — not reinforcing — vehicles' own
  `"שנה"` field name. Both are genuine, non-arbitrary side effects of
  deriving cue words from literal taxonomy strings with no morphological
  awareness, not something patched by hand-excluding specific words.
- **The confidence blend can't guarantee a low score on a maximally
  confident wrong answer.** `LOGPROB_WEIGHT`/`EMBEDDING_WEIGHT` are 0.7/0.3.
  At a maximal logprob signal (~1.0) and a *perfect* embedding mismatch
  (similarity 0.0 — the best the cross-check can ever do), blended
  confidence is still `0.7*1.0 + 0.3*0.0 = 0.7`, above common "trust this"
  thresholds like 0.5
  (`test_out_of_domain_query_disclosed_limitation_maximal_logprob_confidence_still_dominates_the_blend`).
  Item 4 (below) makes the cross-check always run and measurably move the
  score; it doesn't, by itself, guarantee a mismatch reads as *low*
  confidence. Re-weighting toward the embedding signal is a real next step,
  deliberately left as its own explicit tuning decision, not smuggled into
  this pass.

## Confidence methodology

`confidence` is a required response field, and it needs to mean something —
a lower number should correlate with a genuinely less certain extraction.

| Path | Formula | Measured or fixed? |
|---|---|---|
| Rule path | `coverage_ratio * margin_factor` | Measured, per-request |
| LLM Tier 1 / Tier 2 success | `0.7 * logprob_confidence + 0.3 * embedding_similarity` | Measured, per-response |
| Degrade (both tiers fail, or the zero-signal classify call fails) | `0.15` | Fixed constant |

**Rule path**: `coverage_ratio` is the fraction of the query's non-stopword
tokens explained by the winning vertical's matched taxonomy terms/cue
words/numbers; `margin_factor` scales that down when a second vertical
scores close behind (genuine cross-vertical ambiguity should reduce
confidence, not just raw token coverage). Numeric tokens only count as
"explained" once the winning vertical already has at least one genuine
taxonomy *term* match — a cue word alone is too weak a signal to say
whether a bare number is a price, a year, or a km reading.

**LLM tier success**: `logprob_confidence` is `exp(mean(logprob))` over
just the tokens that make up the extracted fields' *values* (not the
surrounding JSON syntax, which sits near-100%-probable regardless of
correctness). `embedding_similarity` is cosine similarity between the
canonical query and a synthetic sentence reconstructed from the extracted
params, **prefixed with the assigned category**
(`"קטגוריה: {vertical}, ..."`) — an out-of-domain extraction (a car query
force-extracted under `נדל״ן`) reads as low-similarity specifically because
of the category mismatch, not just on incidental field-value keywords.
This cross-check now runs on **every** LLM-fallback response, not only
ones a logprob-decisiveness heuristic judged borderline — a model can be
very confident about tokens it typed while being categorically wrong about
what it should have been asked at all. Cost/latency of removing that skip:
the prior implementation's own measurement recorded confidence-calc
overhead at avg 173ms (range 0–462ms), blended across calls that did and
didn't skip the embedding step under the old logic; running it
unconditionally means every LLM-fallback response now pays close to the
non-skipped end of that already-observed range, not the blended average —
plus 2 extra embedding calls (`text-embedding-3-small`, ~49 tokens total)
on calls that previously skipped them, at $0.02/1M tokens: negligible
(~$0.000001/request).

**Degrade**: a fixed `0.15` precisely *because* there's no successful
generation to measure — an honest "this is a rule-path guess, treat it
with real suspicion," not a measurement. The zero-signal classify call
failing is a *different* failure mode from an extraction-tier failure (the
category itself is unknown, not just the fields) and carries its own notes
entry saying so.

## Cost model

Measured, real tokens (`OPENAI_API_KEY` configured, no mocking):

| Call | Prompt tokens | Completion tokens |
|---|---|---|
| Tier 1 extraction (avg of 4 real calls) | 3,323.5 | 191.2 |
| Zero-signal classify-only call (1 real call) | 286 | 8 |

Verified pricing (`developers.openai.com/api/docs/pricing`, Aug 2026, USD
per 1M tokens): `gpt-4.1-nano` $0.10/$0.40, `gpt-4.1-mini` $0.40/$1.60,
`text-embedding-3-small` $0.02/—.

**Tier 1 only** (no escalation): **$0.000410/request**. **Tier 2
escalation**: **$0.001635/request** (estimated by applying Tier 2 pricing
to the same measured token counts — prompt size is dominated by the fixed
system prompt + schema, not the tier). Blended at a conservative 15%
escalation rate (observed ~14% across this project's real testing):
**$0.000655/request**. The classify-only call is a small, additive cost on
top of this *only* for zero-signal queries — at 286/8 tokens and
`gpt-4.1-nano` pricing, ~$0.00003/call, cached at the full-query level
after the first time.

Projecting to 10M queries/month (cache-hit rate and rules-vs-LLM split are
unmeasured without real production traffic — three scenarios, not one
invented-precise number):

| Scenario | Cache hit | Rules share of misses | Monthly cost | $/request (blended) |
|---|---|---|---|---|
| Conservative | 20% | 60% | $2,096 | $0.00021 |
| Moderate | 50% | 60% | $1,310 | $0.00013 |
| Optimistic | 60% | 65% | $917 | $0.00009 |

Even the conservative scenario is ~$2,100/month for 10M queries — the LLM
only ever touches the minority of traffic the rule path can't confidently
resolve. Levers implemented: full-response + word-level normalization
caching, in-flight request coalescing (N concurrent identical requests pay
for one LLM call, not N), the rule-first classifier itself, two-tier
escalation, schema scoping (`llm_fallback_service._scoped_strict_json_schema`,
-8% completion tokens / -12% latency per fallback call, measured). Embeddings
are used narrowly — only for the confidence cross-check, itself only on the
LLM-fallback path — not as the primary classifier, which is cheaper, faster,
and deterministic by design.

## Latency

| Path | Measured |
|---|---|
| Cache hit | p95 ~55ms |
| Rules | p95 ~41ms |
| LLM fallback (Tier 1, uncontended) | avg ~2.6s (range 1.4–4.4s) — **misses the 600ms target** |
| Zero-signal classify + Tier 1 + confidence cross-check (example 9, live) | ~6.0s total |

**Root cause of the 600ms miss, isolated by holding every other variable
fixed:** a plain chat completion with no schema runs ~500-850ms; adding
this service's Structured Outputs strict-mode schema jumps that to
~2,000-3,000ms; `logprobs=True` on top adds ~500ms more. The
schema-constrained decoding itself is the dominant cost — strict mode
requires every property in `required` (nullable unions), so a 20-28-field
taxonomy schema emits that many key-value pairs per call, mostly `null`,
regardless of how few fields the query actually needs. Confirmed by a
controlled experiment: dropping `strict=true` cut average completion
tokens from 192 to 98 and latency by 56% — but also collapsed Hebrew-key
validity from 100% to 12% (garbled, not just incomplete: a real observed
failure was `{"ייחת_היוכנ אפורכן":null}` — scrambled keys), because
without strict mode's constrained decoding this nano-tier model doesn't
reliably reproduce correct right-to-left Hebrew object keys at all. That
variant is rejected on correctness grounds, not adopted for speed.

Time-to-first-token, not generation speed, is the dominant and most
promising lever: a real streaming call measured TTFT at 300-1,700ms
depending on connection warmth, vs. generation itself running a normal
~110-180 tokens/sec. Even a fully warmed connection keeps a 300-500ms TTFT
floor — the model's own prefill cost (processing the ~3,500-token system
prompt + schema) plus whatever setup Structured Outputs' constrained
decoding needs before the first grammar-valid token, not fixable by
warming anything up.

Schema scoping (asking the model only for fields the rule path didn't
already fill) is implemented and adopted — a real, measured -8% completion
tokens / -12% latency / improved validation pass rate — but doesn't close
the 600ms gap on its own; the dominant cost doesn't scale with excluded
field count the way it scales with prompt/schema complexity.

**A separate, narrower finding**: a fresh, cold client hitting a
freshly-started instance can see 200-700ms on its first burst of
concurrent traffic even on the cache/rules path (which does no network
I/O of its own) — reproducible (cold: 205.8ms p95; the same client, warm,
immediately after: 63.0ms), root mechanism not fully identified. A minimal
zero-app-code control test reproduced a matching pattern natively on
Windows but did *not* reproduce it running the same way inside Docker
(matching how this service actually runs) — the native-Windows mechanism
is real but doesn't explain the Dockerized deployment. Practical
mitigation: exercise each code path once at startup before accepting
traffic.

**Not implemented, and why**: the 600ms model-path target is not reachable
by further optimizing the call itself within the current design — every
lever that could plausibly move the number has been tried, measured, and
either adopted (schema scoping) or rejected on correctness grounds
(dropping strict mode). The next architectural step, **deliberately not
implemented here**: write-behind. When rule-path confidence is below
threshold, return the rule path's own result immediately (honestly
sub-threshold), but also kick off the LLM cascade in the background,
writing its result into the cache under the same key once it completes.
The request that triggered the LLM call never waits for it; the next
request for the *same* canonical query — and under Yad2's real Zipfian
traffic, popular searches recur constantly — hits the cache with the
LLM-refined answer, converged on without ever paying LLM latency in a
request's own critical path. The trade-off, stated plainly: the *first*
caller of a genuinely rare query gets the weaker, honestly-labeled
rule-path answer instead of the best available one. That's a real,
disclosed change to what `/parse` promises its caller — a product
decision for whoever owns that trade-off, not a performance tweak to make
unilaterally here.

## Observability

Structured JSON logging (`log_event`) tags every line with `trace_id`;
security events use a distinct `security_`-prefixed `event` tag, greppable
separately. Every OpenAI call (`repositories/openai_repository.py`) logs
under one consistent `event="llm_call_outcome"` tag regardless of
success/failure, with `duration_ms` — and the tier-specific call sites in
`llm_fallback_service.py` (`tier1`/`tier2`/`classify`) log their *own*
`duration_ms` alongside `tier`, so "how long did Tier 1 take" or "how long
did the classify call take" reads off one line, not two correlated by
order. `llm_confidence_service.compute_llm_confidence` logs
`event="confidence_computed"` with both blend components —
`logprob_confidence`, `embedding_similarity`, and which one actually ran
(`embedding_outcome`) — not just the final blended number; a separate
`confidence_embedding_cross_check_unavailable` event fires when the
embedding call fails and the score silently falls back to logprob-only, so
that degradation is visible instead of indistinguishable from a normal
cross-checked response. Every `parse_decision` log line (rules / LLM /
zero-signal-degraded) carries both `confidence` and, where relevant,
`rule_path_confidence` — consistent across all three, not just some.

Prometheus metrics at `GET /metrics`: `parse_requests_total`
(by category), `parse_cache_result_total` (hit/miss),
`parse_model_calls_total` (by `tier` — `tier1`/`tier2`/`classify` — and
`outcome`), `parse_request_duration_seconds` (by path, not blended, so an
SLA violation in one tier can't hide under a healthy aggregate),
`parse_tokens_total` and `parse_cost_usd_total` (by model). `GET /health`
reports `{"status": "ok", "taxonomy_version": "..."}`.
