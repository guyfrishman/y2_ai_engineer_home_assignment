# Design

Architecture, confidence methodology, cost, latency, and known limitations
for the Yad2 Hebrew search-understanding service.

## Architecture

```
sanitize → normalize → cache lookup → rule-based classify + extract
                                              │
                              confidence ≥ 0.58? ──yes──→ return
                                              │no
                                              ▼
                                   LLM fallback (gpt-4.1-nano)
                                              │
                                   success? ──yes──→ return
                                              │no
                                              ▼
                                    degrade to rule-path result
```

Rules run first and free: deterministic taxonomy-term matching handles
the common case — a clear brand, city, or property type — at zero cost.
The LLM is only called when rule-path confidence falls below threshold.
Every field either path can emit is sourced dynamically from
`data/taxonomy.json` via `extra="forbid"` Pydantic models — no code path
can produce a field outside the taxonomy.

Two cases route through a dedicated classification call before extraction:

- **Zero signal** — no taxonomy term or cue word matched anything.
- **A genuine tie** — two verticals scored equally (e.g. `מסחרי`, which is
  both a real vehicle type and a real-estate transaction type).

In both cases, the rule engine's own vertical pick carries no real
evidence — it's an arbitrary tie-break, not a signal — so a single-field
classification call resolves it before extraction runs. Values that
describe a property of an item rather than identifying its category
(condition, fuel type, color) are excluded from classification scoring
entirely, though they still populate their fields once a vertical is
established.

`gpt-4.1-nano` is the fallback model, not the cheaper `gpt-5-nano`: the
entire GPT-5 family rejects `logprobs` requests, and this service's
confidence score depends on them.

The fallback is single-tier by design: one call, and any failure —
network, malformed output, or a schema violation — degrades immediately
to the rule path's own result rather than retrying or escalating. An
earlier two-tier design escalated to a stronger model on select failures;
it was removed after review found no measured evidence the escalation
improved outcomes, at a real, ongoing cost premium.

## Confidence

| Source | How it's produced | Value |
|---|---|---|
| Rule path | Taxonomy-term coverage of the query, discounted when a second vertical scores close behind | Computed per request |
| LLM extraction | Token-level model certainty, capped low if a semantic similarity check disagrees | Computed per request |
| Technical failure | Fixed | 0.15 |
| Explicit "no match" | Fixed | 0.0 |

The LLM-path score blends two signals: the model's own token probability
for the fields it generated, and a cosine-similarity check between the
query and a reconstruction of the extracted answer. A weighted average of
the two cannot function as a veto — at the chosen weighting, the
probability term alone clears the acceptance threshold regardless of how
low the similarity score is. The fix is a hard floor: below a similarity
threshold, confidence is capped low outright, independent of how
confident the model sounded.

This remains an early-iteration heuristic, not a calibrated score. It was
tuned against a small set of observed cases, not a labeled evaluation
set — the honest next step is building one and measuring the signal
against real outcomes rather than intuition.

## Cost

Measured against the real API, `gpt-4.1-nano`:

| Metric | Value |
|---|---|
| Avg prompt tokens / fallback call | 3,323.5 |
| Avg completion tokens / fallback call | 191.2 |
| Cost per fallback request | $0.000410 |

| Scenario | Cache hit rate | Monthly cost (10M queries) |
|---|---|---|
| Conservative | 20% | ~$1,300 |
| Moderate | 50% | ~$820 |
| Optimistic | 60% | ~$570 |

Cost levers implemented: full-response and word-level normalization
caching, rule-first classification (the LLM only ever touches the
minority of traffic rules can't resolve), and schema scoping — asking the
model only for fields the rule path left empty, which measured an 8%
reduction in completion tokens and a 12% latency improvement with no
validation cost.

## Latency

| Path | p95 |
|---|---|
| Cache hit | ~55ms |
| Rules | ~41ms |
| LLM fallback | ~2.6s avg (misses the 600ms target) |

The LLM path does not meet its latency target. Root cause: OpenAI's
Structured Outputs strict mode requires every schema field to be present
in the response, so a 20–28-field taxonomy schema emits that many
key-value pairs on every call, almost all `null`. This dominates the cost
far more than model choice or prompt size.

Two mitigations were tested and rejected on correctness grounds:
disabling strict mode cut latency by over half but dropped Hebrew object
key validity from 100% to 12% — the model doesn't reliably reproduce
correct right-to-left keys without constrained decoding. Schema scoping
(above) helps but doesn't close the gap; the dominant cost scales with
schema complexity, not field count.

The credible fix is architectural, not a further optimization:
write-behind — return the rule path's own result immediately, resolve
the LLM call in the background, and cache it for the next identical
query. This is deliberately not implemented here, because it changes the
response contract's timing semantics, which is a product decision beyond
this assignment's scope.

## Security

- Fixed system prompts; user input is never templated into an
  instruction, only passed as data.
- Every output field is validated against a taxonomy-derived, closed
  schema — nothing outside the allowlist can reach a response.
- Input sanitization strips control characters, emoji, and enforces a
  length cap before any processing.
- A dedicated red-team suite covers prompt injection, malformed input,
  and off-topic queries, which the classifier resolves to an explicit
  `null` category rather than a forced guess.

## Known limitations

- **Some product categories have no taxonomy sector at all** (kitchen
  appliances, for example) — the service degrades gracefully rather than
  forcing a wrong category, but genuinely can't classify what the
  taxonomy doesn't define.
- **Free-text fields (city, street, neighborhood) aren't cross-validated**
  — a query offering two alternatives can occasionally produce a
  concatenated value, since these fields have no enum to catch it.
- **A named brand outside the taxonomy's enum can be substituted with
  the nearest listed option** rather than omitted — a narrower and
  distinct failure mode from the (already-fixed) case of no brand being
  named at all.
- **Cache/rules latency occasionally exceeds its target under concurrent
  LLM traffic**, by a mechanism not fully identified. Eight candidate
  causes were tested and ruled out (logging overhead, the classifier's
  own hot path, confidence-computation cost, CPU/memory saturation,
  server tuning, connection pooling, horizontal replication); the
  remaining hypotheses point toward the local development environment's
  networking layer rather than the application itself, but this hasn't
  been confirmed on production-representative infrastructure.

## Not implemented

- **Write-behind** (see Latency) — the real fix for the latency target,
  scoped out because it changes response-contract semantics.
- **Two-tier LLM escalation** — built, measured, and removed; no
  evidence it improved outcomes over a single well-scoped call, at a
  real and ongoing cost premium.
