# Routers

How the API structures its routes.

## `routers/api.py` — bare composition

```python
from fastapi import APIRouter
from app.routers import search

api_router = APIRouter()
api_router.include_router(search.router, tags=["Search"])
```

Rules:
- Tag goes on `include_router(...)` — a capitalized noun (`Search`).
- No prefix here — the brief calls the endpoint as `POST /parse` verbatim
  and there's no versioned/UI consumer to justify one.

## `routers/<resource>.py` — bare APIRouter, thin handlers, summary on every route

```python
from fastapi import APIRouter, HTTPException, Response, status
from app.schema.requests import ParseRequest
from app.schema.responses import ParseResponse
from app.services.parse_service import parse_query
from app.services.sanitizer_service import QueryRejectedError

router = APIRouter()


@router.post("/parse", summary="Parse a Hebrew free-text search query", response_model=ParseResponse)
async def parse(request: ParseRequest, response: Response):
    try:
        result = await parse_query(request.q)
    except QueryRejectedError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    response.headers["X-Parse-Path"] = result.path
    return result.response
```

Rules:
- `router = APIRouter()` — no prefix, no tag at the router level.
- Every route has `summary=` (shows in Swagger) and, where it returns a
  body, `response_model=`.
- Handler names are **short verb-nouns**: `parse`, `health`.
- No per-handler logging decorator — `main.py`'s middleware sets
  `trace_id` once per request and logs the one exception-boundary event if
  something unhandled reaches it; the service layer logs its own decision
  points. See [logging.md](logging.md) for why a blanket decorator was
  tried and removed.
- Handlers are **thin** — delegate to a service immediately. No taxonomy
  logic, no model calls, no cache access in a router. The one exception is
  translating a domain exception (`QueryRejectedError`) into the right HTTP
  status — that translation is a transport concern, so it belongs here, not
  in the service.
- `async def`, always — `parse_query` is async end-to-end on its only
  real-I/O branch (the LLM fallback), so `await`ing it directly is correct
  and requires no thread offloading. See [llm-usage.md](llm-usage.md) for
  why that matters.

## Top-level mounting

`main.py`:
```python
app.include_router(ping_router)
app.include_router(api_router, dependencies=[Depends(verify_api_key)])
Instrumentator().instrument(app).expose(app)
```

- `/health` is unauthenticated (liveness/readiness probe) and lives outside
  the gated router.
- `/parse` passes through `verify_api_key` (a no-op when no key is
  configured — see [configuration.md](configuration.md)).
- `/metrics` is mounted by `Instrumentator().expose(app)`, unauthenticated —
  scraping tools generally can't send custom headers, and metrics aren't
  secret.

## Path naming

- Lowercase. The brief pins the exact paths (`/parse`, `/health`,
  `/metrics`) — there's no resource-grouping convention to invent beyond that.

## Error handling

- Known errors (empty-after-sanitization query, bad request shape): raise
  `HTTPException` with a clear `detail`, or let FastAPI's own Pydantic
  validation produce the 422.
- Unknown errors: let them bubble — `main.py`'s middleware logs one
  `request_error` event at the boundary and FastAPI returns 500 (see
  [logging.md](logging.md)). This should be rare: the LLM-fallback path's
  own failure modes (bad JSON, invalid schema, network error) are all
  caught and turned into a graceful degrade inside `llm_fallback_service`,
  never a raised exception that reaches the router.

## Why this exact pattern

See [`../decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md`](../decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md)
for the pipeline the router sits in front of.
