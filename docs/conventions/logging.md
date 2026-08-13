# Logging

## Rule

**`@log_activity` on every function in the request call path** — handlers,
services, and repository methods. It's the cheapest observability you'll
ever buy.

The decorator lives in `app/logger.py` (ported near-verbatim from the
template it's modeled on — it's domain-agnostic). It emits structured JSON
with:
- `status`: `STARTING`, `FINISHED`, or `ERROR`
- `process`: the function name
- `session_id`, `trace_id`: pulled from `ContextVar`s, initialized at the
  entry point of each request
- `input` / `output`: arguments and return value, truncated for large
  payloads

Works for both sync and async functions — the decorator detects which and
wraps accordingly, which is why it works unchanged on `OpenAIRepository`'s
async methods.

## `log_metric` — sparse, chartable events

Use `log_metric(...)` for high-level things worth charting, not per-step
state:

```python
log_metric(model=model, total_tokens=usage.total_tokens)
log_metric(event="cache_lookup", result="hit")
log_metric(event="parse_decision", path="rules", vertical=vertical.value, confidence=confidence)
```

These carry `"status": "METRIC"`.

## Security events are a distinct, greppable tag

The brief requires "structured logs for parsing decisions & security
events" — as two things a reader can tell apart, not one undifferentiated
stream. Every security-relevant `log_metric` call uses an `event=` value
prefixed `security_`:

- `sanitizer_service.py` emits `event="security_input_rejected"` whenever
  it strips disallowed characters, truncates an oversized query, or
  rejects an empty-after-sanitization query — with a `reason=` field
  (`disallowed_characters_stripped` / `max_length_exceeded` /
  `empty_after_sanitization`).
- `llm_fallback_service.py` emits `event="security_llm_validation_failed"`
  whenever a tier's output fails schema validation or the API call itself
  fails, tagged with `tier=` and `outcome=`.

Grepping `"event": "security_` isolates exactly the security-relevant
stream from ordinary parsing-decision and metric logs — `grep
'"event": "security_' <logs>` in production, or filter on that field in
whatever log store ingests the JSON.

Ordinary parsing decisions use `event="parse_decision"` and
`event="cache_lookup"` — deliberately un-prefixed, so the two categories
never collide under one grep pattern.

## Don'ts

- **Don't** use `print(...)`.
- **Don't** use the stdlib `logging` module directly — use `app.logger`.
- **Don't** decorate Pydantic models. Decorate functions, not data shapes.
- **Don't** log secrets. The decorator truncates but does not redact — pass
  secrets via env vars, never as function arguments. (`OPENAI_API_KEY`
  never flows into a function argument anywhere in this service — it's
  read directly from `settings` inside `OpenAIRepository`.)

## `LOG_LEVEL`

Default `INFO`. `INFO` emits thin status lines; `DEBUG` adds full
(truncated) inputs and outputs. Keep deployed services at `INFO` to control
volume.

## ContextVar lifetime

`session_id` / `trace_id` are `contextvars.ContextVar`s. The first
decorated function in a call chain initializes them; inner functions
inherit them; the outermost clears them on exit. This propagates correctly
across `await` boundaries in the async LLM-fallback chain — `ContextVar`s
are asyncio-task-local, which is exactly the semantics a per-request trace
id needs.
