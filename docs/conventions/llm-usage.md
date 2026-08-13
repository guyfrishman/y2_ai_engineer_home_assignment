# LLM Usage

This is **not** the provider-agnostic chat-loop pattern a generic AI
template ships with. There's no conversation, no session history, and the
provider is fixed to OpenAI by design — see
[`../decisions/0002-openai-specific-repository.md`](../decisions/0002-openai-specific-repository.md).
What follows is the fallback-tier usage pattern this service actually uses.

## When the model gets called at all

Only when the rule path's confidence is below `settings.confidence_threshold`
(default 0.58) — see [`../decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md`](../decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md)
for the full pipeline. Most realistic queries with a clear vertical signal
and no cross-vertical term overlap (see `docs/examples.md`'s vehicle
examples) resolve on rules alone, at zero model cost.

## The client

`app/repositories/openai_repository.py`:

```python
class OpenAIRepository:
    @classmethod
    async def chat(cls, messages, model, response_format=None, logprobs=False): ...
    @classmethod
    async def embed(cls, text, model=None) -> list[float]: ...
```

Async (`AsyncOpenAI`), not sync — the LLM-fallback branch is the service's
only real network I/O, and it must not block the event loop while waiting
on it. See [routers.md](routers.md) for how that propagates up to the
router.

## The two-tier cascade

`app/services/llm_fallback_service.py`:

1. **Tier 1** (`settings.openai_fallback_model`, default `gpt-4.1-nano`) —
   cheapest suitable model, one attempt, Structured Outputs (`response_format:
   json_schema`, strict mode, schema built from the *same* taxonomy Pydantic
   model the rule path validates against) with `logprobs=True`.
2. **Tier 2** (`settings.openai_escalation_model`, default `gpt-4.1-mini`) —
   only on Tier 1 required-field validation failure or `api_error`. Same
   shape, stronger model.
3. **Degrade** — Tier 2 also failing returns the rule path's own
   (sub-threshold) result with confidence forced to a fixed low constant
   and a notes entry, never a partially-hallucinated LLM structure.

An extra/unknown field in a tier's output is silently stripped by
`extra="forbid"` and does **not** trigger escalation — only a missing or
malformed required field does. Escalating over an unknown field would be
paying for a failure mode the schema already neutralizes for free.

## Rules

- **Talk to OpenAI only through `OpenAIRepository`.** No `AsyncOpenAI(...)`
  anywhere else.
- **Every LLM-facing schema comes from `taxonomy_models.py`.** Never
  hand-write a JSON schema for a fallback call — `_strict_json_schema()`
  derives it from the same Pydantic model the rule path uses, so the two
  paths can never drift into allowing different fields.
- **Fixed system prompts, never templated with user input beyond the
  vertical name.** `app/prompts/system_prompts.py`'s
  `build_extraction_system_prompt(vertical)` only interpolates the vertical
  string, which comes from the classifier, never from the request. See
  that file's docstring for why this, combined with Structured Outputs,
  bounds what a prompt-injection attempt in the query can achieve.
- **Degrade, don't 500.** A missing key, a rate limit, a malformed
  response — none of these fail the request. See "api_error" in ADR 0001.
- **Log and meter token usage** at the point `response.usage` is available
  (inside `OpenAIRepository`), not reconstructed later — see
  [logging.md](logging.md) and [repositories.md](repositories.md).
- **Confidence is measured, not asserted.** A successful tier's confidence
  comes from the completion's own logprobs plus an embedding cross-check —
  see [`../infrastructure/confidence-calibration.md`](../infrastructure/confidence-calibration.md).
  Only the degrade path uses a fixed constant, and that's because there's
  nothing to measure there.

## Prompts

System prompts live in `app/prompts/`. Kept in code (versioned, reviewable),
not a database.
