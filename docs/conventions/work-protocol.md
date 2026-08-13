# Work Protocol

How any non-trivial change gets made here — by humans and coding agents alike.

## The three rules

1. **Plan first.** For anything touching more than one file, write a short
   step-by-step plan before editing. Each step states its deliverable (file,
   function, behavior) and how you'll verify it.
2. **One step at a time.** A step is finished only when it actually works —
   "it compiles" is not "it works." Run it, show the output, then move on.
3. **Stick to the plan.** If you discover something mid-way that demands a
   different approach, stop and re-plan rather than silently improvising.

This is slower on tiny tasks and much faster on real ones. It's the antidote
to ambitious sessions that produce a dozen files, none of which run.

## What a good step looks like

> **Step:** Add the two-tier LLM fallback cascade. New
> `services/llm_fallback_service.py`: Tier 1 call, required-field validation,
> escalation to Tier 2 on failure, degrade to the rule path's own result if
> Tier 2 also fails.
> **Verify:** mocked-`OpenAIRepository` unit tests for all three outcomes
> (tier1 success / tier1-fail→tier2 success / both-fail→degrade), and
> `uv run pytest` stays green.

Bad: "Add the LLM fallback." (No deliverable, no verification.)

This project's own build followed this literally — build order was
taxonomy repository + schema, then pure-function sanitizer/normalizer, then
classifier + extractor verified against worked examples, then the cache
layer, then the LLM fallback (mocked first, real-call verification gated
behind explicit sign-off since it needed a real API key), then the API
surface, then metrics, then the security suite, then the load test, then
the cost model computed from real measured tokens, then docs, then this
README last.

## Suggesting solutions with trade-offs

When the design space has several valid choices, present the options, name
the trade-offs honestly (speed, complexity, risk, cost), recommend one with
reasoning, and let the decision-maker choose. Don't pick silently. If a
decision is architectural, record it in [`../decisions/`](../decisions/).

## Pushback

Honest pushback beats agreement. If a request is technically wrong, say so
with reasons and propose the right thing — then let the decision-maker
decide. This applies in both directions.

## Real external dependencies need explicit sign-off

Some verification steps need something only the decision-maker can
provide — a real API key, a production credential, access to a paid
service. Don't skip that verification silently and don't fabricate what
the result would have been. Stop, ask directly for what's needed, and
continue once it's provided. Everything that *can* be verified without it
should be — a missing dependency blocks one step, not the whole plan.

## Definition of done

A change is done when **all** hold (see also [`../AGENTS.md`](../AGENTS.md)):

- It follows the conventions, or an ADR justifies the deviation.
- `uv run pytest` passes.
- The thing it changes runs end-to-end and the result is inspectable.
- Docs are updated if behavior or a convention changed.

If any of these is missing, it's not done. Don't move on.
