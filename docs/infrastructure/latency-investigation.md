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

## Docker/infra investigation: five more candidates tested, one real bug found elsewhere

The two app-level CPU hypotheses above were ruled out by three orders of
magnitude. This round assumed the cause is infra/networking, not CPU, and
tested five specific candidates directly against a real `docker compose`
deployment (Docker Desktop, Windows, WSL2 backend) with a real
`OPENAI_API_KEY`. `scripts/loadtest.py` gained a `--llm-ratio` flag for
this round — the original fixed 4-query LLM pool collapses into cache
hits (and, with in-flight coalescing, a single shared call) after each
query's first occurrence, so it can't sustain a target ratio of real
fallback traffic over a long run; `--llm-ratio` generates many distinct
sub-threshold real-estate queries (the documented "דירת" construct-form
gap, verified 30/30 below `confidence_threshold` before use) to hold a
requested LLM-path share steady instead.

### 1. Confound check: is this just CPU saturation? No — checked directly

**Where the client runs matters here and wasn't previously stated
explicitly:** `scripts/loadtest.py` runs on the Windows/MSYS host;
Docker Desktop's WSL2 backend runs the container inside a Linux VM. Every
request crosses a virtualized network hop (Windows host → WSL2 VM) that a
bare-Linux Docker host wouldn't have — relevant context for everything
below, not itself proven to be the cause.

`docker stats` polled during a concurrency-20 loadtest run that reproduced
the degradation (cache/rules p95 476.9ms, well past the 150ms target):
**CPU never exceeded 23.4%** of the host's 24 cores (effectively a
fraction of even one core — Docker's `CPUPerc` normalizes so 100% = one
full core saturated). Memory stayed flat around 90MiB. Repeated across
three independent runs (different request counts/concurrency): same
result every time, max observed 25%.

**Conclusion: not CPU-bound.** This matches (and reinforces) the earlier
classifier-hot-path profiling — there is no CPU saturation happening
anywhere during the degraded window. The rest of this investigation
proceeds on that basis.

### 2. Queueing hypothesis: a controlled concurrency × LLM-ratio matrix — inconclusive on its own, but a follow-up test was decisive

**Working hypothesis stated before testing:** a ~2.6s LLM-path request
occupies its slot ~65x longer than a ~40ms rules-path request, so long-held
slow requests might starve capacity for fast ones — a queueing effect, not
a CPU one.

Four runs (150 requests each, fresh cache per run), concurrency ∈ {10, 20}
× LLM-path ratio ∈ {10%, 50%}:

| Concurrency | LLM ratio | cache/rules p95 | LLM p95 |
|---|---|---|---|
| 10 | 10% | 220.4ms (FAIL) | 3,136.5ms |
| 10 | 50% | **56.6ms (PASS)** | 3,314.3ms |
| 20 | 10% | 223.3ms (FAIL) | 2,778.6ms |
| 20 | 50% | 228.6ms (FAIL) | 2,958.4ms |

**This does not cleanly support the stated hypothesis.** At concurrency 20,
raising the LLM ratio from 10% to 50% barely moved cache/rules p95 (223 →
229ms) — if long-held slow requests were starving fast ones proportionally,
5x more of them should have made it meaningfully worse. At concurrency 10,
raising the ratio made it *better* (220 → 57ms), the opposite of the
predicted direction. Reported plainly rather than cherry-picked: **the 2×2
matrix does not show a clean, monotonic relationship with either variable
individually.**

**A follow-up test, not originally planned, was decisive where the matrix
wasn't.** Inspecting the full cache-path latency distribution (not just
p50/p95) for one degraded run (concurrency 20, ratio 10%, n=129 cache
requests) showed it is sharply **bimodal**, not a smooth shift:

```
min/p10/p25/p50/p75/p90/p95/p99 (ms): 9.2, 13.9, 44.1, 48.3, 52.2, 343.4, 365.6, 366.3
count > 100ms: 26 (of 129)
top 10 slowest (ms): 364.5, 364.8, 365.0, 365.6, 365.6, 365.7, 365.9, 366.2, 366.3, 366.3
```

112/129 requests land in the normal 9-52ms band; 17 requests cluster
**tightly within a 1.8ms band around 365ms** — nearly identical values,
the signature of a batch of requests released together by one shared
blocking condition, not independent per-request jitter.

**Direct A/B test: same concurrency (20), LLM ratio 10% vs. 0% (pure
cache/rules traffic, zero LLM-path queries).** At ratio 0%: **0/144 cache
requests over 100ms**, p95 51.1ms, clean pass. At ratio 10%: 26/129 over
100ms, p95 223ms. This is unambiguous: **the stall only appears when
concurrent LLM-path traffic is present.** The queueing hypothesis's core
mechanism — slow in-flight requests affecting fast ones' latency — is
confirmed causally, even though the small 2×2 matrix's specific dose-response
wasn't clean (likely small llm-call counts, 15-75 per run, combined with
incidental interleaving order under a deterministic shuffle — timing luck,
not a refutation of the mechanism).

**What remains open:** *which* shared resource the stalled requests are
blocked on isn't pinned down by this test alone. The tight clustering
argues for something that releases a batch of waiters at once (a pool,
queue, or proxy layer) rather than a per-request cost. Items 3-5 test the
most likely candidates directly.

### 3. Uvicorn tuning (`--backlog`, `--limit-concurrency`, `--timeout-keep-alive`) — no reproducible improvement, and a real 503/reset trade-off confirmed

Added to the `Dockerfile` `CMD`: `--backlog 4096` (2x uvicorn's default
2048), `--limit-concurrency 100` (uvicorn's own default is unlimited),
`--timeout-keep-alive 5` (matches uvicorn's default, made explicit).

Same reproducer scenario (concurrency 20, ratio 10%) run 3x untuned and 3x
tuned, fresh cache each time:

| | Untuned p95 | Tuned p95 |
|---|---|---|
| Run 1 | 223.3ms | 633.2ms |
| Run 2 | 458.4ms | 218.8ms |
| Run 3 | 215.6ms | 263.0ms |

**No reproducible improvement.** The tuned and untuned ranges overlap
completely (215-458ms untuned, 219-633ms tuned) — run-to-run variance is
larger than any effect the tuning produced. `--backlog`/`--timeout-keep-alive`
at these values don't move the needle on this bottleneck.

**`--limit-concurrency` set too low is a real, severe failure mode —
demonstrated, not just asserted.** Set to 5 (deliberately low) and hit
with concurrency-20 pure cache/rules traffic (no LLM calls, so this is
cheap to demonstrate): **133 clean 503s + 2 hard connection resets out of
150 requests — a 90% failure rate.** The connection resets
(`httpcore.ReadError` on the client) also crashed `loadtest.py` itself,
since `_send_one` didn't catch transport-level errors — fixed alongside
this (now counted as a `connection-reset` outcome in the failure
breakdown rather than aborting the whole run). Restored to `100` (well
above any concurrency this service has been tested at) before continuing.
**This is exactly the trade-off to watch for in production: a
"safety" limit set without headroom turns a slow-request problem into an
outright-rejected-request problem, and not even always as a clean 503.**

### 4. AsyncOpenAI connection pool — the premise didn't hold; checked directly, not assumed

**Checked before configuring anything:** does `OpenAIRepository`'s
`AsyncOpenAI` client actually use bare httpx defaults
(`max_connections=100, max_keepalive_connections=20`)? **No.**
`openai` 3.0.0's own `_constants.py` sets
`DEFAULT_CONNECTION_LIMITS = httpx2.Limits(max_connections=1000,
max_keepalive_connections=100)` on its vendored `httpx2` transport — 5x
more generous than the commonly-cited bare-httpx numbers the premise
assumed. This service's own test concurrency (max 20, and at most one real
call in flight per distinct query thanks to in-flight coalescing) never
gets remotely close to even the *unconfigured* limit.

Implemented anyway, for completeness and because the user-facing ceiling
should be a documented, intentional value rather than an SDK internal a
future reader has to go looking for: explicit `httpx2.Limits(max_connections=2000,
max_keepalive_connections=200)` passed via `AsyncOpenAI(http_client=...)`
in `openai_repository.py`. Re-ran the reproducer scenario twice: 639.2ms,
216.4ms — same overlapping range as every other variant tried, no
distinguishable effect. **Exactly the result predicted once the premise
was refuted** — this was not expected to help, and it didn't.

### 5. Multi-replica validation — the big one, and the most surprising result

Temporarily stood up three replicas (`api1`, `api2`, `api3`, each the
unmodified service image) behind an `nginx:1.27-alpine` reverse proxy on
its own port, to actually test — not just assert — "scale via replicas,
not `--workers`" (this doc's earlier sections, `docs/services/search-api.md`'s
Quirks section). Explicit per-replica names, not Compose `deploy.replicas`
plus nginx's own hostname-based upstream resolution: nginx resolves a
proxied hostname once at startup, not per request, without the dynamic
`resolver` directive, so a naive `proxy_pass http://api:8000` against a
scaled service can silently pin to one replica instead of actually
load-balancing. Verified round-robin directly (6 `/health` requests →
2/2/2 split across the three containers' own logs) before trusting any
latency numbers from it. This was throwaway validation infrastructure for
this one investigation, not a shipped deployment shape — torn down after
the run below; the default deployment stays the single-instance
`docker-compose.yml`.

Same reproducer scenario (concurrency 20, ratio 10%), 3 runs, fresh cache
each time:

| Run | Single-instance p95 (item 3/4 baseline) | 3-replica p95 |
|---|---|---|
| 1 | 223-633ms (range across items 3-4) | 656.1ms |
| 2 | " | 239.6ms |
| 3 | " | 219.1ms |

**No reproducible improvement from 3x the capacity.** The multi-replica
range (219-656ms) sits inside the same noise band every single-instance
variant already showed. This is a genuinely surprising, negative result:
tripling backend capacity behind a working load balancer did not fix the
degradation.

**The cache-hit-rate cost predicted in advance did materialize, exactly as
expected:** each replica keeps its own independent in-memory cache — there
is no shared cache across replicas. The `rules` path count (fresh
classifications, not cache hits) went from **6 on a single instance to 18
across three replicas** — a 3x increase, because a repeated query that
would hit one shared cache now independently misses on whichever replica
nginx happens to route it to first. **Real cost, no matching latency
benefit measured in this environment.**

**What this result means for the open question:** if adding independent,
fully-capable replicas behind a real load balancer doesn't move cache/rules
p95 at all, the bottleneck most likely sits *upstream of every replica* —
shared infrastructure all instances sit behind equally, not per-instance
capacity. The strongest remaining candidate, consistent with item 1's
explicitly-flagged confound: the Docker Desktop WSL2 network translation
layer between the Windows-host test client and the Linux containers (or
the test client's own connection handling under concurrent slow+fast
request mixing) — something this investigation cannot isolate further
without either a bare-Linux Docker host (no WSL2/Windows NAT hop) or a
loadtest client run *inside* the Docker network instead of from the host,
both worth a dedicated follow-up.

### Summary: five candidates tested this round, none fixed the degradation

| Candidate | Result |
|---|---|
| CPU saturation | Ruled out — max 23-25% of 24 cores |
| Concurrency vs. LLM-ratio (raw dose-response) | Inconclusive on its own; superseded by the direct A/B test below |
| LLM traffic present vs. absent (same concurrency) | **Confirmed causal** — 0/144 vs. 26/129 requests over 100ms |
| Uvicorn `--backlog`/`--timeout-keep-alive` tuning | No reproducible improvement |
| Uvicorn `--limit-concurrency` set too low | Confirmed severe trade-off (90% failure rate at limit=5) — not the fix, a new risk to avoid |
| AsyncOpenAI connection pool widening | Premise refuted (SDK already generous); no effect, as predicted |
| 3x replicas behind a real load balancer | No reproducible improvement; confirmed cache-hit-rate cost |

Two candidates are now ruled out from the previous round
(`@log_activity`, the classifier hot path), five more from this round
(CPU, connection-pool sizes on both the server and OpenAI-client side,
backlog/keep-alive tuning, and — the biggest structural lever available —
horizontal scaling itself). The degradation is real, reproducible, and
now substantially narrowed: it is not caused by anything this codebase's
own request-handling code does, and it does not respond to adding more
independent instances of that code. The leading remaining hypothesis is
infrastructure shared across all instances alike — most concretely, the
Docker Desktop WSL2 networking layer sitting between the test client and
every container in this environment — which would also explain why
scaling replicas (all still behind the same host-level Docker Desktop
networking stack) didn't help. Confirming that specific layer needs a
different test environment (bare-Linux Docker, or a loadtest client
co-located inside the Docker network) than this investigation had
available, and is recorded here as the concrete next step, not left
unstated.

## Primary cause identified; one compounding factor confirmed open — not fully resolved

An earlier version of this section claimed the cache/rules p95
degradation was **resolved** as a generic cold-connection-pool artifact.
A direct reconciliation against this investigation's own earlier
LLM-ratio A/B result (Track A, item 2: 0/144 vs. 26/129 requests over
100ms, same concurrency, only LLM-path traffic presence differing) showed
that claim doesn't fully hold up. This section keeps what's still true,
states plainly what broke, and reports the corrected, narrower
conclusion — including that a real mechanism remains genuinely
unidentified, not just under-explained.

### What's still true: a real cold-start artifact exists — on native Windows

**A minimal control app — zero lines of this project's code — reproduces
a bimodal pattern.** A bare FastAPI app with two routes (`/fast`
returning instantly, `/slow` doing `await asyncio.sleep(2.6)` — no
cache, no classifier, no OpenAI client, no middleware), run **natively
on the Windows host** (not in Docker), driven by the same concurrency-20
semaphore-limited `httpx` client pattern `loadtest.py` uses: **18/129
`/fast` requests clustered tightly at 280-287ms**, the rest at 6-8ms.
Correlating client and server wall-clock timestamps for the same delayed
requests showed server-side handler execution was 0.000ms and response
transmission was 1-4.5ms — the ~280ms sat entirely in the gap before the
server's handler started running. The IDs of every delayed request were
exactly the first 18 submitted, and the effect appeared even with **zero
slow requests at all** (pure-fast, 129-request cold-pool run, still ~20
stuck at ~289ms), and disappeared on a second round against the same,
now-warm client (0/129 stuck). Nagle's algorithm was checked directly in
CPython's own source (`selector_events.py`, `proactor_events.py` both
call `_set_nodelay()` on every transport by default) and ruled out as the
mechanism.

**All of that is real and reproducible.** It's a genuine native-Windows
`asyncio`/`httpx` cold-connection-burst characteristic. The mistake was
concluding it explains the Dockerized real application's degradation
without checking whether it actually does.

### The reconciliation check, run directly

**First: did the earlier "no-LLM" A/B run (0/144 over 100ms) use an
already-warm client?** No — checked directly. It ran
`docker compose restart api` immediately beforehand and used a fresh
`uv run python -c "..."` process (a new `httpx.AsyncClient`, cold pool),
identical in structure to the degraded 10%-ratio run. Re-running it and
inspecting **every** path bucket, not just `cache` (the earlier check's
blind spot — the handful of genuine first-hits land in `rules`, which
was never separately checked): `rules: n=6, max=44.7ms, 0 stuck`,
`cache: n=144, max=58.7ms, 0 stuck`. Nothing was hiding in the bucket
that wasn't checked before. The cold-client premise holds; the "already
warm" explanation for the discrepancy does not.

**Second: does connection-establishment alone (no LLM traffic, but
without the small-query-pool/heavy-caching structure of the original
ratio-based test) reproduce it on the real app?** Generated 129 distinct
real high-confidence vehicle queries (`מאזדה CX-5 שנת 2020`-shaped,
varying brand/model/year — no repeats, so no caching effect dilutes the
sample the way 6 repeated texts did in the original test) and ran them
against a freshly-restarted real container, cold client, concurrency 20,
zero LLM traffic: **`rules: n=129, max=55.8ms, 0 stuck`.** Query
diversity wasn't the confound either — cold-start connection
establishment alone, on the real Dockerized app, does not produce
clustering, no matter how the non-LLM traffic is shaped.

**Third — the decisive check: does the minimal control app *itself*
reproduce the effect when run the same way the real app actually
runs, inside Docker?** The same minimal app (unchanged) built into a
container and driven identically (concurrency 20, cold client, fresh
container): **pure-fast, zero slow requests: 0/129 stuck.** Mixed
fast+`asyncio.sleep(2.6)`-slow: **0/129 stuck.** Both flatly contradict
the native-Windows result under otherwise-identical test structure — the
cold-connection-burst artifact that's real and reproducible on native
Windows **does not reproduce inside Docker at all.**

**Ruling out what's different about a real slow request, one variable at
a time.** Since Docker itself seemed to be the boundary, two more
candidates were tested directly on the Dockerized minimal app rather
than assumed: replacing the synthetic `asyncio.sleep` with a **real**
outbound HTTPS call to `api.openai.com` (a genuine minimal chat
completion, real DNS/TLS/network round trip, same host the real app
talks to) — **0/129 stuck.** Adding `prometheus-fastapi-instrumentator`
to the same real-OpenAI-call variant (matching the real app's own
per-request instrumentation middleware, which the minimal app otherwise
lacks entirely) — **0/129 stuck.** Neither outbound network activity
from inside the container nor Prometheus's request middleware is the
missing ingredient.

### The honest, corrected conclusion

**The cause is not resolved. It is narrowed, and several plausible
mechanisms are now ruled out with direct evidence rather than assumed
away:** not Nagle, not generic Docker outbound-networking contention, not
`prometheus-fastapi-instrumentator`'s middleware, not query diversity or
caching structure in the test itself, and — per this section's whole
point — not the generic native-Windows cold-connection-burst mechanism
either, since that specific mechanism demonstrably does not reproduce
inside Docker. What remains genuinely unexplained is **why the full real
application, specifically, shows the effect when a real OpenAI call is
in the traffic mix, when a minimal app making the same kind of real call
inside the same Docker environment does not.** The real application
differs from the minimal repro in ways not yet tested in isolation: a
much larger prompt (~3,500 tokens vs. "say hi"), Pydantic validation of a
20-28-field taxonomy model, logprob math over up to 400 tokens, an
embedding cross-check on some calls, structured JSON logging on every
request, and the `openai` SDK's own client (`AsyncOpenAI`) rather than
raw `httpx`. Any of these — or some combination — is the credible next
place to look, not yet isolated within this session's budget.

**What still stands, independent of the unresolved mechanism:** the
direct warm-vs-cold test against the real application itself (not the
minimal repro) is untouched by this reconciliation — a cold client
hitting a freshly-restarted container failed the SLA (cache/rules p95
205.8ms); the same client, warm, immediately after, against the same
server, passed cleanly (p95 63.0ms). That observation is real regardless
of which specific mechanism causes it. `scripts/loadtest.py`'s
connection-pool warm-up (added on the strength of the now-corrected
"resolved" conclusion) is kept — it's harmless, and pre-warming before
timing is sound test methodology regardless of the exact mechanism — but
its justification is now "reduces a real, if not fully explained,
first-burst effect on this specific application," not "eliminates a
generic, fully-understood artifact."

**Practical implication, stated at the honest confidence level this
reconciliation actually supports:** a genuinely cold service does show
elevated cache/rules latency on its first burst of concurrent traffic
when real LLM-path calls are in the mix — confirmed, real, reproducible
on the actual application. It is **not** caused by Nagle, generic Docker
networking, or Prometheus instrumentation — those are now ruled out with
evidence, not just deprioritized. It does **not** correlate with the
native-Windows cold-connection-burst mechanism this investigation
initially (incorrectly) generalized from. The production recommendation
— exercise each code path once at startup before accepting traffic —
still holds as a reasonable mitigation, but is no longer backed by a
fully-understood root cause, and should be described that way.

## Track B: the LLM call itself — decomposed, and one real optimization adopted

Track A (above) resolved the cache/rules-path mystery. This track returns
to the still-unmet 600ms model-path target and asks two focused
questions: where inside a single ~2.6s Tier 1 call does the time actually
go, and does scoping the wire schema down help.

### B1. Time-to-first-token vs. generation rate, via a real streaming call

A non-streaming call only reports total latency — it can't distinguish
"the model took a long time to start responding" from "the model
responded promptly but took a long time to finish." A real streaming
Tier 1 call (`stream=True`, same schema, same model, same live API)
separates the two directly.

**First call, cold:**

| Phase | Measured |
|---|---|
| Time-to-first-token (TTFT) | **1,666.6ms** |
| Generation (first → last token, 212 chunks) | 1,878.3ms (~113 chunks/sec) |
| Total | 3,544.9ms |

**TTFT is the majority of the call, not the generation phase** — the
model (or the request pipeline in front of it) spends more time producing
*nothing* than it spends producing the actual ~450-character response.
Per the task's own framing: a TTFT this large (>>300ms) raises the
question of whether Track A and Track B share a root cause, and it's
worth checking directly rather than assumed away.

**Three more calls, same already-open client (warm connection):**

| Call | TTFT | Total | Chunks |
|---|---|---|---|
| 1 (still effectively cold — first real call of this process) | 767.6ms | 1,932.5ms | 208 |
| 2 | 358.8ms | 1,463.3ms | 202 |
| 3 | 493.9ms | 1,642.6ms | 204 |

**TTFT drops substantially on a warm connection (767ms → 359-494ms,
roughly 40-55%) but does not disappear.** This is a genuine partial
overlap with Track A's finding: some of TTFT is connection/TLS-handshake
cost to OpenAI's real servers, paid once and amortized on reuse, exactly
like the loopback connection-burst cost Track A found — but a substantial
floor (300-500ms) remains even fully warm. That floor isn't explained by
connection state; it's the model's own prefill cost (processing the
~3,500-token system prompt + schema) plus whatever setup Structured
Outputs' constrained decoding needs before it can emit the first
grammar-valid token — a cost intrinsic to the request, not fixable by
warming anything up.

**Conclusion: TTFT, not generation speed, is the dominant and most
promising lever left** — generation itself (~110-180 chunks/sec measured
here, broadly consistent with this project's earlier ~70 tokens/sec
estimate from a different, non-streaming measurement) is not obviously
throttled. But the warm-connection floor (300-500ms) means even a
perfectly warmed-up, schema-optimized call has a real prefill cost this
investigation did not find a lever for within budget — consistent with
the B3 conclusion below.

### B2. Schema scoping — measured, and adopted

**The idea:** the LLM only needs to fill fields the rule path's own
(sub-threshold) extraction left empty — not re-derive fields already
known deterministically. Build the Structured Outputs schema from just
the unfilled subset instead of the full 20-28-field model, merge the
rule path's already-known fields back into the LLM's response, and
validate the *merged* result against the *full*, unscoped taxonomy model
— scoping narrows what's asked for on the wire, never what's allowed
through.

**Measured against the golden query set** (8 queries across all 3
verticals, real Tier 1 calls, same model/settings both variants):

| | Baseline (full schema) | Scoped (gap fields only) | Delta |
|---|---|---|---|
| Avg latency | 1,512ms | 1,337ms | **-11.6%** |
| Avg completion tokens | 187 | 172 | **-8.1%** |
| Validation pass rate | 62% (5/8) | 75% (6/8) | **improved, not degraded** |

Per-query, the fields actually excluded from the wire schema were small
(the rule path typically already has 1-3 fields right even below
threshold) — e.g. 28→25, 20→16, 28→27 — so this is a modest, not
dramatic, token/latency win. The validation-pass-rate improvement (n=8,
not large enough to be a tight confidence interval, but directionally
consistent with the mechanism) makes sense structurally: fewer fields for
the model to get wrong, and the fields it doesn't have to touch (already
correct, from the deterministic rule path) can't introduce a mistake.

**Per the task's own decision rule** (adopt if it doesn't degrade
validation; report and don't adopt if it does): this is a clean win on
every axis measured, not a trade-off to weigh. **Adopted** —
`llm_fallback_service.py`'s `_call_tier` now builds a scoped schema per
call (`_scoped_strict_json_schema`, falling back to the full schema in
the edge case where the rule path already has every field), merges the
LLM's response with the rule path's known fields, and validates the
merge against the full model. Confidence scoring was updated alongside
it: `compute_llm_confidence`'s logprob signal now scores only the fields
the model itself generated (`llm_returned_fields`), not the merged
result — scoring a field the model was never asked about would silently
find nothing in the raw completion text and skip it, which happens to be
harmless but isn't the intent; the embedding cross-check still uses the
full merged params, since that's the complete answer being sanity-checked
semantically. Regression tests: `tests/test_llm_fallback_service.py`'s
`test_scoped_schema_narrows_what_is_asked_not_what_is_allowed` (proves
merging-then-validating-against-the-full-model actually happens, using
the sector/subcategory cross-field validator as the one check that only
fires on the complete object), `test_scoped_schema_excludes_already_known_fields_from_the_wire_schema`,
and `test_scoped_schema_falls_back_to_the_full_schema_when_nothing_is_left_to_scope`.

**Why this doesn't close the 600ms gap on its own:** an 8-12% reduction
on a ~2.6-3.5s call is real money and a real, if modest, latency
improvement — not a fix for a target that needs roughly an 80% reduction.
B1's finding explains why: the dominant cost is TTFT (prefill +
constrained-decoding setup), which scales with prompt/schema *complexity*
more than a handful of excluded fields meaningfully changes, not with
completion length alone.

### A real bug found along the way, unrelated to the p95 question: cancelling the resolving request could hang coalesced waiters forever

Reviewing shutdown behavior for `parse_service.py`'s in-flight request
coalescing (a graceful shutdown cancels whatever task is still resolving
when the grace period elapses) surfaced a genuine gap, confirmed by direct
reproduction before fixing: the code that settles the shared
`asyncio.Future` on failure caught `except Exception`, but
`asyncio.CancelledError` is a `BaseException`, not an `Exception`, since
Python 3.8 — so a cancelled resolver never settled its future at all.
Any concurrent request coalescing onto that same future (`await
existing_future`) would then hang **indefinitely**, since nothing else
was ever going to resolve it. Reproduced directly with a controlled
test before fixing (see `tests/test_parse_service.py`'s
`test_cancelling_the_resolving_request_does_not_hang_coalesced_waiters`):
confirmed the hang, then confirmed the fix (catching `BaseException`,
wrapping a bare `CancelledError` as a plain `RuntimeError` so it doesn't
bleed an unrelated cancellation into the waiter's own task) resolves it in
microseconds instead. See `docs/conventions/repositories.md`'s coalescing
section and `y2_ai_search_api/services/parse_service.py` for the fixed code.
