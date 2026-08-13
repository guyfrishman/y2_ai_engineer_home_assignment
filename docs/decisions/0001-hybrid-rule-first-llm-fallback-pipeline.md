# 0001 - Hybrid rule-first, two-tier LLM-fallback pipeline

**Decision:** Every query runs through sanitize → normalize → cache lookup →
rule-based classify+extract first, entirely without a model call. Only when
the rule path's confidence falls below `confidence_threshold` (0.58) does
the pipeline call OpenAI, and even then as a two-tier cascade — a cheap
model (`gpt-4.1-nano`) first, escalating to a stronger one (`gpt-4.1-mini`)
only if the cheap model's output fails the taxonomy's own required-field
validation, never further than that. If both tiers fail, the pipeline
degrades to the rule path's own (sub-threshold) result with a fixed low
confidence and a notes entry, rather than either failing the request or
returning a broken LLM structure.

Rule-path confidence is `coverage_ratio * margin_factor`: the fraction of
the query's non-stopword tokens explained by the winning vertical's matched
taxonomy terms/cue-words/numbers, scaled down when a second vertical scores
close behind (genuine cross-vertical ambiguity, e.g. a color value that's
valid in both `רכב` and `יד_שנייה`, should reduce confidence, not just the
raw token coverage). A successful LLM tier's confidence is *measured*, not
a hardcoded band: the geometric mean of the completion's own logprobs over
just the extracted VALUE tokens (not JSON structure, which is
near-100%-probable regardless of correctness), blended 70/30 with a cosine
similarity between the query and a synthetic sentence reconstructed from
the extracted params. Only the final degrade path uses a fixed constant
(0.15) — there's no successful generation to measure there, so a fixed low
number is an honest "unknown," not a measurement. Full detail:
`docs/infrastructure/confidence-calibration.md`.

An extra/unknown field in a tier's JSON is silently stripped by the
taxonomy Pydantic model's `extra="forbid"` and does **not** trigger
escalation — only a genuinely missing or malformed required field does.
Both tiers use OpenAI Structured Outputs (`response_format: json_schema`,
strict mode) built directly from the same Pydantic model the rule path
validates against, so the two paths can never drift into allowing
different fields.

**Why:** The brief's own SLA split (p95 ≤150ms cache/rules, p95 ≤600ms
model path) is a design spec, not just a target — it implies most traffic
should never touch a model at all. Rules are free, deterministic, and fast;
they handle the common case (a clear brand/city/property-type match) with
zero marginal cost per request. The LLM is reserved for what rules
genuinely can't do — recognizing "אייפון" implies Apple/cellular-phones is
product knowledge no taxonomy lookup provides (see `docs/examples.md`'s
iPhone example). Cheap-tier-first is safe specifically *because* Structured
Outputs enforces valid JSON syntax regardless of model size — the risk of
starting with the cheapest model is bounded to semantic extraction errors,
which strict Pydantic validation against the taxonomy already catches for
free. Degrading instead of failing means an OpenAI outage, a missing key,
or a rate limit never turns into a 500 — the service always returns
*something* usable, just at a level of confidence that says so honestly.

`gpt-4.1-nano`/`gpt-4.1-mini`, not the nominally cheaper `gpt-5-nano`/`gpt-5-mini`
— verified live against the real API (not assumed from documentation): the
entire GPT-5 family rejects `logprobs` requests outright (403 "not allowed
to request logprobs from this model"), and this pipeline's LLM-tier
confidence score depends on logprobs. GPT-4.1-nano is the cheapest model
confirmed, live, to support both Structured Outputs strict mode and
`logprobs` together — see `docs/infrastructure/cost-model.md` for the
resulting real measured token/cost figures.

A second live-testing finding shaped the taxonomy schema itself, not just
model choice: a real Tier 2 call once paired `תת_קטגוריה: "מחשבים_ניידים"`
(laptops) with `סקטור: "מוסיקה_וכלים"` (music) — individually valid enum
values, but a nonsensical combination that still passed per-field
validation. `UsedGoodsParams` now carries a cross-field
`model_validator` rejecting exactly that mismatch, so a wrong-but-allowlisted
category pairing is a validation failure (and correctly triggers escalation
or degrade) rather than silently succeeding.

**Satisfies:** "Any approach is allowed (rules, models, embeddings,
hybrids). Must justify your choices and optimize for cost at Yad2 scale";
latency targets (p95 ≤150ms cache/rules, p95 ≤600ms model path); "classifier
+ selective calls" as a named cost-reduction option; "Strict JSON Schema
validation"; "allowlisted fields/categories"; confidence as a required
response field.

## Future direction: write-behind / optimistic degrade (not implemented)

The measured LLM-fallback latency (`docs/infrastructure/latency-investigation.md`:
~2.6s for an isolated Tier 1 call alone, before any escalation) means the
600ms model-path target isn't reachable by making the model call faster
within the current request-response contract — see the root README's
honest latency writeup. The next architectural step worth considering, **not
implemented here**, is removing the LLM call from the request's own
critical path entirely:

**The idea:** when rule-path confidence is below threshold, return the
rule path's own result immediately (same as today's degrade path — sub-threshold
confidence, honest about it), but *also* kick off the Tier 1/Tier 2
cascade in the background, writing its result into the cache under the
same key once it completes. The request that triggered the LLM call never
waits for it. The next request for the *same* canonical query — and at
Yad2's real scale, popular searches recur constantly, a Zipfian
distribution where a small number of distinct queries account for a large
share of traffic — hits the cache and gets the LLM-refined answer,
converged on without ever paying LLM latency in a request's critical path.

**Why this is the right next step, not a nice-to-have:** it doesn't try to
make the model faster (this ADR's diagnosis shows that's a serving-side
constraint, not something this codebase controls) — it changes *who pays*
for that latency. Under Zipfian traffic, p95 across all requests collapses
to the rules path's latency (already meeting the 150ms target), because
the LLM tier's cost gets paid once per distinct rare query and amortized
across every repeat, off the critical path.

**The trade-off, stated plainly:** the *first* caller of a genuinely rare,
long-tail query — one no other user has asked yet — gets the weaker,
lower-confidence rule-path answer, honestly labeled as such, not the
LLM-refined one. Today's synchronous design guarantees every caller gets
the best available answer, at the cost of that caller sometimes waiting
several seconds for it. Write-behind guarantees every caller gets a fast
answer, at the cost of first-time askers of rare queries getting a weaker
one. That's a real semantic change to what `/parse` promises its caller —
a product decision (is "always fast, sometimes provisional" an acceptable
contract, versus today's "always best-effort, sometimes slow"), not a
performance tweak to make unilaterally inside this service. Recorded here
as the recommended next step for production scale, with the trade-off
explicit, for whoever owns that decision to make.
