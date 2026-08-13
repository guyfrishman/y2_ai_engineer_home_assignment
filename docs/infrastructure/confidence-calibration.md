# Confidence calibration

`confidence` is a required response field, and it needs to mean something —
a caller (or a human reviewing logs) should be able to trust that a lower
number really does correlate with a less certain extraction. This page
explains how each path computes it and why.

## Three sources, not one formula

| Path | Formula | Measured or fixed? |
|---|---|---|
| Rule path | `coverage_ratio * margin_factor` | Measured, per-request |
| LLM Tier 1 / Tier 2 success | `0.7 * logprob_confidence + 0.3 * embedding_similarity` | Measured, per-response |
| Degrade (both tiers fail) | `0.15` | Fixed constant |

## Why flat hardcoded LLM confidence bands were rejected

An earlier version of this design used fixed bands — a flat `0.70` for any
Tier 1 success, `0.80` for any Tier 2 success. That was rejected: a flat
constant reports the *same* number whether the model was genuinely
confident or guessing, which defeats the point of a per-response
confidence field. A caller filtering on `confidence > 0.75` should get
requests the model was actually more sure about, not an artifact of which
tier happened to answer.

## The logprob signal

`llm_confidence_service.compute_logprob_confidence` requests `logprobs=True`
on the chat completion, then isolates the tokens that make up the
extracted **values** — not the surrounding JSON syntax (`{`, `"`, `:`, field
names), which sits near-100%-probable regardless of whether the value is
correct and would dilute the signal if included. It reconstructs the
completion's raw text from its token stream (with per-token character
offsets), locates each present field's value span using
`json.JSONDecoder.raw_decode` (which understands full JSON grammar, so
nested values like a `{"min":...,"max":...}` range are located correctly
without manual brace-counting), and computes
`exp(mean(logprob for token in value_tokens))` — the geometric mean of
each value token's probability.

Verified directly (`tests/test_llm_confidence_service.py`): fabricating a
completion where only the value tokens are low-probability yields a
measurably lower score than an equivalent completion with confident values,
while the surrounding structure is held identical — confirming the
isolation actually works, not just that the formula runs.

## The embedding cross-check

A model can be high-confidence about a wrong extraction — logprobs alone
can't catch that. `compute_embedding_similarity` reconstructs the extracted
params as a synthetic Hebrew sentence (`"מחיר: max=70000 צבע: לבן"`),
embeds it and the canonical query with `text-embedding-3-small` (the
cheapest available embedding model), and takes their cosine similarity as
an independent semantic sanity check.

## The blend

`confidence = 0.7 * logprob_confidence + 0.3 * embedding_similarity`.
`LOGPROB_WEIGHT`/`EMBEDDING_WEIGHT` in `llm_confidence_service.py` are named
constants, documented there as an **initial, tunable choice** — not a fixed
law. If production data showed the embedding cross-check catching cases
the logprob signal misses (or vice versa), the honest next step is
re-weighting against a labeled sample, not treating 0.7/0.3 as sacred.

If the embedding call itself fails (e.g. no `OPENAI_API_KEY`), a successful
extraction isn't thrown away — `compute_llm_confidence` falls back to the
logprob signal alone rather than failing the whole tier over an unrelated
API call.

## The degrade path is categorically different

When both tiers fail required-field validation, there is no successful
generation to measure — no logprobs, no params to embed. `DEGRADED_CONFIDENCE
= 0.15` in `llm_fallback_service.py` is a single fixed constant precisely
*because* it's not standing in for a measurement; it's an honest "this is
the rule path's own sub-threshold guess, treat it with real suspicion."

## Correlation check

`docs/examples.md`'s worked set spans both paths: `test_extractor_service.py`
asserts the rule-path formula produces higher confidence for queries with
clear, unambiguous taxonomy-term coverage (`מאזדה CX-5 שנת 2020 גיר אוטומטית
דיזל`, confidence 0.86) than for queries with genuine cross-vertical term
overlap (`דירת 3 חדרים בירושלים...`, confidence 0.5 — "ירושלים" is valid
both as a `נדל״ן` city and a `יד_שנייה` region). That ordering matching
manual judgment of which query is actually less ambiguous is the informal
correlation check for the rule path. The equivalent check for the LLM-tier
logprob+embedding formula is `test_confidence_reflects_value_token_uncertainty_not_structure`
in `tests/test_llm_confidence_service.py`, run against fabricated
low/high-confidence completions rather than a live model (no key is
available at test time) — once a real key is configured, the honest
follow-up is spot-checking that real completions with an obviously
mismatched extraction (caught by hand) score lower than clean ones.
