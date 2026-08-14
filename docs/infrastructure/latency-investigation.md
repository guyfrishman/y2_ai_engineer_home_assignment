# Latency investigation: output-volume-bound or grammar-bound?

The root-cause writeup in `docs/services/search-api.md` and the README
established that Structured Outputs strict-mode schemas add ~2-3 seconds
over a bare chat completion, but stopped short of explaining *why*: is the
model generating a lot of output tokens (fixable by emitting less), or is
per-token decoding itself slow under a large enum grammar (fixable only by
a smaller/different grammar)? This page answers that from data already
logged in this session — no new API calls.

## The data

`OpenAIRepository.chat` already logs `completion_tokens` via `log_event`
on every call. Two independent real samples from this session's actual
Tier 1 (`gpt-4.1-nano`) calls, both against this service's real taxonomy
schemas:

**Completion tokens** — 5 real calls (from the cost-model measurement
batch and an earlier schema-verification call):
```
151, 212, 145, 203, 205  →  mean 183.2 tokens
```

**Tier 1 latency** — 10 real, non-escalated calls (from the sequential
per-phase diagnostic in the prior latency writeup):
```
mean 2,613ms
```

**Honest caveat:** these are two separate batches of calls (different
queries), not one paired dataset — this session doesn't have a run that
logged both `completion_tokens` and wall-clock latency for the *same*
calls. Combining their means as an approximation is the best available
answer from already-logged data without spending anything new; it's not a
tight per-call correlation.

**Derived: ~70 tokens/sec** (183.2 tokens / 2.613s).

## Interpretation

The task framework: ~250+ completion tokens points to
**output-volume-bound** (strict mode pads every optional field as an
explicit `null`, so a 20-28-field schema emits that many key-value pairs
per call regardless of how many the query actually supports); ~40 tokens
but still ~2,600ms points to **grammar-bound** (per-token constrained
decoding overhead from large enums, independent of how much is generated).

183.2 tokens sits far closer to the 250-token output-volume threshold
(distance 67) than to the 40-token grammar threshold (distance 143) — six
times closer to the former than the latter.

The derived generation rate, ~70 tokens/sec, is also informative on its
own: that's a normal-to-healthy decoding speed for a nano-tier model, not
the kind of severely throttled per-token rate (typically well under
20-30 tokens/sec) you'd expect if a large enum grammar were making *each*
token expensive to sample. A grammar-bound service would show a low
tokens/sec figure even for a short completion; this doesn't.

## Conclusion: OUTPUT-VOLUME-BOUND

The dominant cost is generating a full, mostly-null 20-28-field JSON object
on every call, not slow per-token decoding under the enum grammar. At
~70 tokens/sec, ~183 tokens of output alone accounts for ~2.6s — closely
matching the measured Tier 1 average almost in full, leaving little room
for a separate large "grammar tax" on top.

**Consequence for the fix:** the lever that matters is "emit less," not
"constrain less." In the V0-V4 experiment below, **V1 (`strict=false`, no
null-padding) is predicted to be the dominant win**; V2 (enum fields as
plain strings in the wire schema, still Pydantic-validated after parsing)
is predicted to matter far less, since the grammar-cost hypothesis this
diagnosis rules out is exactly what V2 would address. This prediction is
stated before running the experiment, so the experiment can be read as a
real test of it, not a post-hoc rationalization.

## A correctness fix, and a finding that didn't confirm its own hypothesis

Before running the experiment, `OpenAIRepository`'s `AsyncOpenAI` client
was given an explicit `timeout=5.0s` and `max_retries=0`. It had been
using the SDK's own defaults (`max_retries=2`, `timeout=600s`) — meaning
every latency number in this project up to this point could have silently
included up to three sequential attempts with backoff, and a genuinely
hung call could have blocked a request for ten minutes.

Re-running the same 12-call sequential benchmark after the fix:

| | Before (SDK defaults) | After (`timeout=5s`, `max_retries=0`) |
|---|---|---|
| Tier 1 avg latency | 2,613ms | 2,864ms |
| Tier 1 max latency | 4,359ms | 5,018ms |
| Escalation rate | 17% (2/12) | **42% (5/12)** |
| Tier 2 avg latency | 3,127ms | 3,472ms |
| Confidence-calc avg | 173ms | 280ms |

The task's working hypothesis was that silent retries were a strong
candidate for the 4,359ms outlier specifically, and that removing them
would show up as a lower mean and a tamer outlier. **That's not what the
data shows** — average latency didn't drop (if anything it rose slightly)
and the max latency is still right at the new timeout boundary (5,018ms
against a 5.0s ceiling). What changed sharply instead is the **escalation
rate: 17% → 42%**.

Two explanations are consistent with that pattern, and this data can't
cleanly separate them:

1. **Silent retries were masking transient Tier 1 failures.** A brief
   rate-limit or transient error that the SDK used to retry-and-succeed
   invisibly now surfaces immediately as a failure, correctly triggering
   this service's own Tier 2 escalation instead of the SDK papering over
   it. Under this explanation, the new 42% is a more *honest* number, not
   a worse system.
2. **5.0s is tight enough to cut off legitimate slow completions.** Real
   Tier 1 calls were already landing close to 5s even before this change
   (the old max was 4,359ms). A completion that would have finished
   successfully at 5.3s now times out and counts as a Tier 1 failure,
   inflating the escalation rate for a reason that has nothing to do with
   retries.

Both are plausible; twelve calls per side isn't enough to tell them apart,
and this project's ~$1 experiment budget wasn't spent trying to (that
would need a controlled run holding `max_retries=0` fixed while varying
the timeout alone, ideally at a larger sample size than used here). The
honest conclusion: the fix itself is correct regardless of which
explanation dominates — silently trusting a chat API's default 600s
timeout and 2 hidden retries in a service that already has its own
tier-escalation-as-retry logic was a real bug — but it traded a smaller,
harder-to-see problem (unmeasured retry time) for a larger, visible one
(more Tier 2 escalations), and 5.0s specifically should be treated as a
starting point worth revisiting with a dedicated timeout-sweep, not a
tuned final value.

## The V0-V4 experiment

Fixed set of 8 short queries (chosen to minimize prompt-token cost and
variance — see `QUERIES` in the experiment script), identical across every
variant, run sequentially. `max_completion_tokens=300` capped throughout.
Validation is always checked against the real, unmodified strict taxonomy
model (`extra="forbid"`, `Literal` enums) regardless of what schema was
sent on the wire — that's what "validation pass rate" measures: would this
response have survived this service's actual production validation step.

| Variant | Config | Avg latency | Avg completion tokens | Validation pass rate |
|---|---|---|---|---|
| V0 | baseline: `strict=true`, full enums, `logprobs=true` | 3,241ms | 192 | **8/8 (100%)** |
| V1 | `strict=false`, same schema | 1,438ms (**-56%**) | 98 | **1/8 (12%)** |
| V2 | `strict=true`, enums stripped from wire schema | 2,989ms (-8%) | 189 | 6/8 (75%) |
| V3 | V1 + V2 | 1,064ms (spot-check, n=3) | 9 | 1/3 (33%) |
| V4 | V0 + `logprobs=false` | 2,281ms (**-30%**) | 192 | **8/8 (100%)** |

### V1/V3 rejected — not a speed/accuracy trade-off, a correctness collapse

V1 is 56% faster, which looks like a strong win on latency alone. It
isn't one: inspecting the raw failures (`v1_failure_inspect.py`) shows the
failure mode isn't truncation or a missed-field omission — it's **garbled
Hebrew object keys**. A real V1 response for `"אוטו טוב"`:
```
{"ייחת_היוכנ אפורכן":null,"סוגי רכב":null}
```
Neither key is a real taxonomy field name; one is scrambled Hebrew, one
has control characters mixed into a corrupted Unicode escape sequence.
Without strict mode's constrained decoding forcing the model to reproduce
the schema's exact key names, this nano-tier model doesn't reliably
generate correct right-to-left Hebrew object keys at all — it's not a
matter of missing values, the *structure itself* comes back wrong. This
reframes what strict mode is actually buying here: not just "all fields
present," but the ability to correctly emit Hebrew keys in the first
place, for this specific model.

Per the task's own decision rule — a variant that materially raises
validation failures is rejected regardless of speed, because more
escalations mean worse latency anyway — V1 and V3 (which inherits the same
failure mode, 33% pass rate on a 3-call spot-check) are both disqualified.

### V2 rejected — no speed benefit, and it costs correctness anyway

V2 confirms the Task 1 diagnosis directly: stripping enum constraints from
the wire schema (while keeping `strict=true`, so the null-padding and key
structure are unchanged) produces **no meaningful latency change** (2,989ms
vs. 3,241ms — within the noise band this project has already seen
run-to-run). If per-token enum-grammar cost were the dominant factor, this
should have shown a clear win; it didn't, which is further, independent
confirmation that the bottleneck is output volume, not decoding grammar.
It also *costs* something for no benefit: with no enum constraint, the
model produced two out-of-taxonomy values in 8 calls, dropping validation
to 75%.

### V4: real, and the only variant that's both faster and safe

V4 (drop `logprobs`, keep everything else about V0) is **30% faster
(2,281ms vs. 3,241ms) with identical 100% validation** — confirming the
earlier isolated measurement (~500ms) at a larger sample (n=8, ~960ms
delta here).

### Recommendation: no schema/logprobs config change in production, and here's why

By the task's own rule (latency first, unless validation materially
worsens), V4 "wins." It is **not adopted as the new production default**,
for a reason outside the rule as stated: this service's confidence score
(`docs/infrastructure/confidence-calibration.md`) is architecturally built
on the completion's logprobs — dropping `logprobs=true` doesn't just save
time, it removes the primary signal the whole measured-confidence design
depends on, degrading every successful Tier 1/Tier 2 response to
embedding-similarity-only confidence. That's a real architectural
trade-off, not a free 30% win, and changing it deserves its own explicit
decision rather than falling out of a latency experiment. **Recorded here
as the clearly-quantified trade-off it is** (30% latency for the logprobs
signal) so a future decision to relax the confidence design can point at
real numbers instead of a guess.

What *did* ship from this experiment: `max_completion_tokens` capped on
both tiers (implemented regardless of variant outcome, as a safety bound —
see `llm_fallback_service.py`), and the embedding-cross-check skip-when-
decisive optimization (`llm_confidence_service.py`) — both real,
unconditional improvements independent of which schema variant "won."

## The G0-G3 experiment: does GPT-5 change the answer?

`logprobs=true` is the current production setting — V4 (drop `logprobs`,
keep everything else) won on latency in the experiment above but was
deliberately not adopted, because this service's confidence score is
built on the completion's logprobs. This round asks a different question:
is there a model that's both fast *and* reliable enough that dropping
`logprobs` (which GPT-5 requires, since it rejects `logprobs` requests
outright) becomes worth it on its own merits?

### Premise re-verified before spending anything

Both re-checked live, not assumed from the first investigation:
- `gpt-5-nano` and `gpt-5-mini` are still valid, listed model IDs
  (confirmed via `client.models.list()` — alongside a lot of newer
  siblings: `gpt-5.1` through `gpt-5.6`, none tested here, out of scope
  for this round).
- The `logprobs` 403 still reproduces, byte-identical error message, on
  both models: `"You are not allowed to request logprobs from this model"`.

Premise holds. Proceeding with all four variants at `logprobs=false`.

### Results

Same 8-query fixed set as the V0-V4 experiment, sequential,
`max_completion_tokens=300`, validated against the real strict taxonomy
model regardless of what schema was sent on the wire.

| Variant | Model | strict | Avg latency | Avg completion tokens | Tokens/sec | Validation pass rate |
|---|---|---|---|---|---|---|
| G0 (baseline = V4) | gpt-4.1-nano | true | 1,962ms | 190 | **96.7** | 88% (7/8) |
| G1 | gpt-5-nano | true | 2,932ms (+49%) | 234 | 79.9 (-17%) | 62% (5/8) |
| G2 | gpt-5-nano | false | 1,628ms | 72 | 44.3 | **0% (0/8)** |
| G3 | gpt-5-mini | false | 1,343ms | 41 | 30.8 | 50% (4/8) |

**The tokens/sec bet did not pay off — measured, not inferred.** The
question this round was actually deciding: does GPT-5 generate faster
per-token than GPT-4.1, which would make its higher sticker latency at
matched token counts less of a concern? No. GPT-5-nano is **17% *slower*
in tokens/sec than GPT-4.1-nano** (79.9 vs 96.7), and GPT-5-mini slower
still (30.8). Whatever GPT-5-nano's `reasoning_effort=minimal` mode is
spending time on, it isn't generating the visible completion faster.

### G1 (gpt-5-nano, strict mode) — strictly worse, not a trade-off

Slower (+49%), more tokens (234 vs 190 — strict mode's null-padding cost
applies here too, and apparently more of it), and worse validation (62%
vs 88%) than the current baseline, simultaneously. Not a latency-vs-
correctness trade-off to weigh — it loses on every axis measured. One
observed failure mode worth noting: `מצבי_עסקה` (transaction types) came
back as `["מכירה","השכרה","שותפים","מגרשים","מגרשים"]` for the query
"משהו זול" ("something cheap") — a real estate transaction-type list,
inserted for a query that named no transaction type at all, with one
value duplicated and another (`מסחרי`) missing. Reads like the model
partially dumping the enum rather than extracting from the query.

### G2 (gpt-5-nano, strict=false) — the capability hypothesis, tested and refuted at this tier

The premise going in: V1's failure (gpt-4.1-nano garbling Hebrew object
keys without constrained decoding) might be a nano-tier-specific
capability gap, not inherent to dropping strict mode — so a different
model family might not have it. **It's worse, not absent.** 0/8 valid,
and the raw failures are more severely garbled than V1's, not less:

```
'טלפון זול'  -> {" ":null}
'אוטו טוב'   -> {"סוג_יְכוֹל":"אוטו טוב"}
'משהו זול'   -> {"מזגי_כספים":null}
'בית יפה'    -> {"מצעי_אספשׁה":{...,"title":"מצעי نشست"}, ...}
```

None of these keys are real taxonomy fields, several aren't real Hebrew
words at all, one embeds a literal null control character as a key, and
one mixes in Persian/Arabic script (`نشست`, "session/sitting") inside a
Hebrew field name — a cross-script hallucination that wasn't observed in
V1. The hypothesis was directionally wrong at the nano tier: this is not
"gpt-4.1-nano specifically is bad at Hebrew keys," it's "small models
without constrained decoding are unreliable at Hebrew keys," and GPT-5-nano
is not an exception.

### G3 (gpt-5-mini, strict=false) — the hypothesis holds *directionally*, not sufficiently

Triggered automatically per the run plan, since G2 failed validation.
Validation improves markedly over G2 (50% vs 0%) — real evidence that a
stronger model garbles Hebrew keys less, supporting the capability
hypothesis as a *gradient*, not a binary. But 50% is still far below the
88% baseline, and the failures that remain are a qualitatively different,
softer kind — near-miss spelling variants of real field names rather than
pure fabrication (`חנייה` vs. the taxonomy's `חניה`, `מפר_מגרש` vs.
`מ״ר_מגרש`, `תיאור` — an invented field whose *value* was literally the
string `"RealEstateParams"`, apparently echoing the Pydantic class name
back rather than the field name). The "successful" responses were also
suspiciously sparse — several passed validation at only 14 completion
tokens, consistent with a near-empty `{}`-like object rather than a
useful extraction; passing validation and being a *good* extraction
aren't the same thing, and this round's pass-rate numbers don't
distinguish them.

### Verdict: no variant qualifies: production defaults unchanged

The plan called for reporting a logprobs-removal trade-off analysis if
any variant won on both latency and validation. **None did.** G1 loses on
both axes outright. G2 fails validation completely. G3 improves on G2 but
still trails the current baseline on validation (50% vs 88%) while its
latency edge is undermined by how uninformative its passing responses
look. G0 — the current production configuration in every dimension except
`logprobs` (already `true` in production, tested here at `false` only to
keep this comparison apples-to-apples with G1-G3) — remains the best
measured option.

Per instruction, **production defaults were not changed** as a result of
this experiment; `reasoning_effort` was added to `OpenAIRepository.chat`
as an optional parameter (unused by default) purely so this comparison
could be run without a signature change blocking it later.

Since no candidate qualified, the planned confidence-signal replacement
analysis (embedding-only vs. rule-path-agreement vs. a blend) doesn't have
a concrete trade-off to weigh this round — it would only become relevant
if a future candidate actually won on both axes. For a future attempt: the
natural next comparison this round didn't cover is `gpt-5-mini` **with**
`strict=true` (G1 only tested nano at strict mode) — plausible given G3
showed mini meaningfully outperforms nano's Hebrew-key reliability even
without constrained decoding, so mini *with* constrained decoding might
beat G0 outright, at gpt-5-mini's higher per-token price.

## Second ruled-out candidate for the cache/rules p95 degradation: the classifier hot path

`classifier_service._scan_term_occurrences` scans the canonical query
against every known taxonomy term (241 compiled regex patterns) on every
rules-path request — O(number of taxonomy terms) per call, pure CPU, on
the single-threaded event loop. A plausible-looking candidate for the
still-unexplained cache/rules p95 degradation under load (200-700ms
against a 150ms target, on a path with no network I/O): `@log_activity`
was already ruled out, and this is the other real CPU-bound cost on that
path.

**Profiled before touching any code, per the instruction not to assume.**
Isolated timing across the 8-query golden set (200 iterations per query,
warm cache):

| Measurement | Mean | p95 |
|---|---|---|
| `_scan_term_occurrences` alone | 0.107ms | 0.124ms |
| Full rules pipeline (sanitize+normalize+classify+extract) | 0.140ms | 0.157ms |

`_scan_term_occurrences` is ~96% of `classify_query`'s own cost, but
`classify_query` itself is a rounding error next to the pipeline's other
stages. Worst-case aggregate: even in the pathological case of 20
concurrent requests all arriving simultaneously and serializing entirely
behind each other on the GIL, total queueing across the whole batch is
**~2.8ms** (20 × 0.14ms) — three orders of magnitude smaller than the
200-700ms actually observed.

**Conclusion: not the cause. Investigated and ruled out, not fixed** — no
n-gram rewrite was implemented, since profiling shows there's no
meaningful cost here to recover. Replacing a working, simple O(terms)
scan with a more complex O(query_tokens × max_ngram) dictionary lookup
would trade "boring and obvious" for a real engineering cost (correctness
risk around the existing longest-match-wins and span-consumption
semantics) to save microseconds nothing is waiting on.

Two candidates are now ruled out (`@log_activity`, the classifier hot
path); the actual mechanism behind the cache/rules degradation under
concurrent LLM load remains an open question, still most likely
infra-level contention (client connection pooling, or Docker Desktop's
networking layer under sustained external call volume) rather than
anything in this codebase's own CPU-bound work.
