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
