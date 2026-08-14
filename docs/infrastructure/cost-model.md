# Cost model

## Measured base numbers

Token counts below are real, measured from live calls against the running
service (`OPENAI_API_KEY` configured, no mocking) — not estimated. Four
Tier 1 (`gpt-4.1-nano`) fallback calls, triggered by real sub-threshold
queries from `docs/examples.md`:

| Call | Prompt tokens | Completion tokens |
|---|---|---|
| 1 | 3,518 | 212 |
| 2 | 2,753 | 145 |
| 3 | 3,511 | 203 |
| 4 | 3,512 | 205 |
| **Average** | **3,323.5** | **191.2** |

Each fallback also makes two embedding calls (`text-embedding-3-small`) for
the confidence cross-check — query + synthetic-params-sentence — averaging
**49 tokens total** across both.

Verified pricing (`developers.openai.com/api/docs/pricing`, Aug 2026, USD
per 1M tokens):

| Model | Input | Output |
|---|---|---|
| gpt-4.1-nano (Tier 1) | $0.10 | $0.40 |
| gpt-4.1-mini (Tier 2) | $0.40 | $1.60 |
| text-embedding-3-small | $0.02 | — |

## Per-request cost

**Tier 1 only** (no escalation): `3323.5 × $0.10 + 191.2 × $0.40` (÷1M) +
embedding cost = **$0.000410/request**.

**Tier 2 escalation** doesn't have a clean measured sample in this run (all
four calls happened to resolve on Tier 1) — estimated by applying Tier 2
pricing to the *same measured token counts* (the schema+prompt structure
is identical between tiers, only the model differs), since prompt size is
dominated by the fixed system prompt + JSON schema, not the tier: **$0.001635/request**.

Across this project's real testing (7 live fallback calls total, 1
escalated to Tier 2), the observed escalation rate was ~1/7 ≈ 14% — too
small a sample to trust precisely, so the blended estimate below rounds
that up to **15%** as a deliberately conservative assumption:

`blended_fallback_cost = 0.85 × $0.000410 + 0.15 × ($0.000410 + $0.001635) = $0.000655/request`

## Projecting to 10M queries/month

The unknowns are cache-hit rate and what fraction of cache-misses resolve
via rules vs. the LLM fallback — this project has no real production
traffic to measure those from, so three scenarios are shown rather than one
invented-precise number:

| Scenario | Cache hit | Rules share of misses | LLM requests/mo | Monthly cost | $/request (blended) |
|---|---|---|---|---|---|
| Conservative | 20% | 60% | 3,200,000 | $2,096 | $0.00021 |
| Moderate | 50% | 60% | 2,000,000 | $1,310 | $0.00013 |
| Optimistic | 60% | 65% | 1,400,000 | $917 | $0.00009 |

The "rules share of misses" figures (60–65%) come from this project's own
verified example set (`docs/examples.md`): 5 of 8 worked examples resolve
via rules alone. That's a proxy from a curated set of realistic queries,
not a random sample of real user traffic — the honest caveat on every
number in this table.

**Headline: even in the conservative scenario, ~$2,100/month serves 10M
queries** — the LLM only ever touches the minority of traffic the rule
path can't confidently resolve; cache hits and rule-path resolutions are
architecturally free.

## Cost-reduction levers — implemented vs. discussed

**Implemented:**
- **Full-response caching** (`cache_repository.py`) — a repeated query
  never re-triggers classification, extraction, or a model call.
- **In-flight request coalescing** (`parse_service.py`) — closes the gap
  full-response caching alone leaves open: N *concurrent* identical
  requests arriving before the first has finished (and cached) its
  result — the realistic shape of a newly-popular query under real
  traffic — now pay for exactly one LLM call between them, not N.
- **Word-level normalization caching** (`functools.lru_cache` on
  `normalizer_service.correct_word`) — a second, smaller cache layer for
  the typo/fuzzy-correction step specifically, independent of whether the
  full query has been seen before.
- **Classifier + selective calls** — the entire architecture: rules first,
  LLM only below `confidence_threshold`. This is the single largest lever;
  every scenario above assumes the majority of traffic never reaches a
  model call at all.
- **Two-tier escalation, not always-strongest-model** — Tier 1
  (`gpt-4.1-nano`, cheapest model confirmed to support both Structured
  Outputs and logprobs) handles the large majority; Tier 2
  (`gpt-4.1-mini`) only runs on Tier 1's actual validation failures (~15%
  of fallback traffic in this measurement).
- **Schema scoping** (`llm_fallback_service._scoped_strict_json_schema`)
  — the wire schema on a fallback call only asks for fields the rule path
  didn't already fill, not the full per-vertical field set. Measured on
  the golden query set: **-8% completion tokens** per fallback call (a
  direct, proportional cost reduction on top of the levers above), with a
  *higher*, not lower, validation pass rate. See
  `docs/infrastructure/latency-investigation.md`'s schema-scoping section
  for the full experiment.

**Falls out of the design for free, not separately implemented:**
- **Prompt compression via OpenAI's automatic prompt caching.** The system
  prompt + JSON schema sent on every fallback call is fixed per vertical —
  identical across many different user queries. OpenAI automatically
  discounts cached-prefix input tokens on repeated prompts over the
  platform's caching threshold. This project's own measured calls show
  `cached_tokens: 0` in the response usage (the four measurement calls were
  spaced out during manual testing, not fired in the rapid succession that
  keeps a prompt cache warm) — so the cost table above is a **conservative
  baseline that does not assume this discount**. At sustained production
  QPS, with the same fixed prompt hit repeatedly within the cache's
  retention window, this would reduce Tier 1/Tier 2 input-token cost
  further, without any code change.

**Deliberately not used for the main extraction path:**
- **Embeddings-vs-rules.** Rule/fuzzy-matching was chosen over an
  embedding-based classifier for the *primary* extraction path because
  it's cheaper (zero extra API calls on the hot path), deterministic (the
  same query always classifies the same way, which a nearest-neighbor
  embedding match doesn't guarantee), and it's what makes the ≤150ms
  cache/rules-path SLA achievable at all — an embedding call alone costs
  more latency than that whole budget. Embeddings are used narrowly: only
  for the LLM-tier confidence cross-check
  (`docs/infrastructure/confidence-calibration.md`), itself only on the
  already-rare fallback path (2 embedding calls per fallback, ~49 tokens
  total — negligible next to the chat completion's ~3,300 prompt tokens).

## What would change this model

- **Real production cache-hit and rule/LLM-split data**, once this service
  has actual traffic — replacing the three-scenario table with one
  measured number.
- **A larger sample of Tier 2 escalations** — the current 15% assumption
  is conservative-by-round-number, not a tight confidence interval.
- **Sustained-load prompt-cache verification** — confirming `cached_tokens`
  actually goes non-zero under realistic concurrent traffic, and by how
  much.
