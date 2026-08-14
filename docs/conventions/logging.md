# Logging

## One function: `log_event`

`y2_ai_search_api/logger.py` exposes a single entry point:

```python
from logger import log_event

log_event(event="cache_lookup", result="hit")
```

Every call emits one structured JSON line, tagged with the current
request's `trace_id`:

```json
{"trace_id": "8155ba08-...", "event": "cache_lookup", "result": "hit"}
```

## `trace_id` comes from middleware, not from a decorator

`main.py`'s `trace_id_middleware` sets `trace_id` once, at the actual
entry point of every request — a fresh UUID per request, stored in a
`ContextVar` so every `log_event` call anywhere in that request's call
graph picks up the same value without threading it through every function
signature. It's asyncio-task-local, so it survives `await` boundaries
correctly (including across the async LLM-fallback chain).

## The four event categories

Everything this service logs falls into one of these. Use the existing
`event=` value for the category you're adding to; don't invent a fifth
without discussing why the existing four don't fit.

| `event=` | Where | What |
|---|---|---|
| `cache_lookup` | `cache_repository.py` | Every cache read, `result=hit\|miss` |
| `parse_decision` | `parse_service.py` | Rule-path or LLM-path resolution, with vertical/confidence/tier |
| `request_completed` | `parse_service.py` | Once per successful request: `path`, `latency_ms`, `category`, `confidence`, and the query text — **the one place the query is logged**, once, not per function |
| `llm_call_outcome` | `openai_repository.py`, `llm_fallback_service.py` | A tier's call succeeded or hit `api_error` |
| `security_*` | `sanitizer_service.py`, `llm_fallback_service.py` | Anything security-relevant — see below |

Plus one boundary event outside this table: `request_error`, logged once
by the middleware if a request raises an exception that reaches it (see
below).

Token-usage lines from `openai_repository.py` (`model`, `prompt_tokens`,
`completion_tokens`, `total_tokens`, no `event=`) are the one deliberate
exception — raw metric data, not a decision point, so they don't carry a
category tag.

## Security events are their own, greppable prefix

Every security-relevant event uses an `event=` value prefixed
`security_`:

- `sanitizer_service.py` emits `security_input_rejected` whenever it
  strips disallowed characters, truncates an oversized query, or rejects
  an empty-after-sanitization query, with a `reason=`.
- `llm_fallback_service.py` emits `security_llm_validation_failed`
  whenever a tier's output fails schema validation or the API call
  itself fails, tagged with `tier=` and `outcome=`.

`grep '"event": "security_'` isolates exactly this stream from ordinary
parsing-decision and metric logs — the brief's "structured logs for
parsing decisions & security events" requirement, satisfied by two
genuinely distinguishable tag families, not an undifferentiated stream.

## The error boundary

`main.py`'s middleware is also the one place an unhandled exception gets
logged:

```python
@app.middleware("http")
async def trace_id_middleware(request, call_next):
    token = trace_id_var.set(str(uuid.uuid4()))
    try:
        return await call_next(request)
    except Exception as error:
        log_event(event="request_error", exception_type=type(error).__name__)
        raise
    finally:
        trace_id_var.reset(token)
```

A routine, already-handled rejection (an empty query → `QueryRejectedError`
→ `HTTPException(400)` inside `search.py`'s router) never reaches this —
FastAPI's own exception handling converts it to a response before it
propagates this far, so only a genuinely unexpected failure (something
that would otherwise become a 500) produces a `request_error` line.

## Why `@log_activity` (a decorator on every function) was removed

An earlier design decorated every handler, service, and repository
method, emitting a `STARTING`/`FINISHED` (or `ERROR`) pair per call.
That's roughly 15-20 log lines for a single successful `/parse`
request — several of them logging the same session/trace context
redundantly, most of them duplicating latency and error-rate data
Prometheus already aggregates correctly (percentiles, rates) in a way a
log line can't. At 10M queries/month that's a real, avoidable volume cost,
and every one of those calls ran `extract_inputs` → recursive
`truncate_value` → `json.dumps` — real per-call CPU work for output mostly
never read.

It's also strictly worse for tracing a failure than the current design:
`@log_activity` produced one `ERROR` line **per function on the call
stack**, so a single failure could log the same exception multiple times
on the way up. The middleware boundary above logs it exactly once, with
the same `trace_id` every other line from that request already carries.

**This was tested, not just argued.** Removing `@log_activity` was floated
as a candidate fix for cache/rules-path p95 degrading under concurrent
load (a `json.dumps` + truncation walk on every call is real, if small,
per-request overhead competing for the same event loop as concurrent LLM
requests). Re-running the loadtest at the same concurrency after removal
showed no improvement — see `docs/services/search-api.md`'s Quirks section
and the root README's latency writeup for the measured result. The
decorator was still removed, on its own merits (volume, duplication,
per-call cost), but the specific "this is why cache/rules degrades under
load" theory didn't hold up and isn't claimed here.

## What was also removed, and why

- **`session_id`.** Carried over from an early conversational design,
  where it identified a chat session. `/parse` is stateless — there's no
  session — so it logged `"n/a"` on every single line. Gone from the
  `ContextVar`, the log output, and this doc.
- **Per-function decoration generally.** See above.

## Don'ts

- **Don't** use `print(...)`.
- **Don't** use the stdlib `logging` module directly — use `logger.py`'s `log_event`.
- **Don't** log secrets. `log_event` truncates long values
  (`settings.max_str_log_length`, default 200 chars) but does not redact —
  `OPENAI_API_KEY` is read directly from `settings` inside
  `OpenAIRepository` and never flows into a `log_event` call.
- **Don't** log the query text anywhere except the one `request_completed`
  call. Search queries are user input; logging them repeatedly per
  request is both a volume problem and a privacy one.

## `LOG_LEVEL` and `MAX_STR_LOG_LENGTH`

Both read from `config.settings` (`settings.log_level`,
`settings.max_str_log_length`), not `os.getenv` directly — see
[configuration.md](configuration.md). `logger.py` importing from
`config` (and not the reverse) is what keeps this import-cycle-free;
`config.py` has no reason to import `logger.py` and doesn't.
