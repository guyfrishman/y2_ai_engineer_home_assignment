# Repositories

External I/O — the taxonomy file, the cache, and OpenAI — sits behind a
repository. This is the seam that keeps the rest of the code testable and
swappable.

## The three repositories

| Repository | Interface | Shipped implementation |
|---|---|---|
| `TaxonomyRepository` | concrete (loads once, read-only) | in-memory, loaded from `data/taxonomy.json` |
| `CacheRepository` | abstract base (`abc.ABC`) | `InMemoryTTLCache` |
| `OpenAIRepository` | concrete, OpenAI-specific | `AsyncOpenAI`-backed client |

### `TaxonomyRepository` — the allowlist, loaded once

`y2_ai_search_api/repositories/taxonomy_repository.py` reads `data/taxonomy.json` at
import time and builds:
- per-vertical Pydantic params models (`taxonomy_models.py`'s
  `build_*_params_model` functions), each `extra="forbid"`;
- a flat term index (`term_index`) mapping every known taxonomy value to
  the `(vertical, field_name)` it belongs to — what the classifier scores
  coverage against and the extractor fills fields from;
- `taxonomy_version`, a content hash of the file, used as part of the
  cache key so editing the taxonomy invalidates stale cache entries for
  free (see [`../decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md`](../decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md)).

There's no write path and no swappable-backend concern here — this is
read-only, versioned configuration data, not a datastore. It's still a
repository (not a bare module-level dict) because it owns the one piece of
I/O (reading the file) and the one-time construction cost.

### `CacheRepository` — storage behind an interface

```python
class CacheRepository(ABC):
    @abstractmethod
    def get(self, key: str) -> dict[str, Any] | None: ...
    @abstractmethod
    def set(self, key: str, value: dict[str, Any]) -> None: ...
```

`InMemoryTTLCache` backs it with `cachetools.TTLCache` — bounded size,
time-based expiry, no hand-rolled eviction logic. A single module-level
instance is what the service layer imports:

```python
cache_repository: CacheRepository = InMemoryTTLCache()
```

**In-flight request coalescing lives in `parse_service`, not here** — a
`cache_key -> asyncio.Future` dict tracking requests that have missed the
cache but haven't written a result yet. Without it, N identical requests
arriving concurrently (a newly-popular query fanning out under real
Zipfian traffic) would all miss the cache and all pay for their own LLM
call — a stampede. The first caller for a key resolves normally; every
concurrent duplicate awaits that same in-flight resolution instead of
redoing the work, reported with its own `path="coalesced"` in metrics/logs
so a fast coalesced wait is never conflated with a genuine fresh
rules/LLM resolution. See `parse_service.parse_query` and
`tests/test_parse_service.py`'s coalescing tests.

**The owning request's future must be settled even if its own task is
cancelled, not just on a normal exception.** A graceful shutdown cancels
whatever task is still resolving once the grace period elapses; since
`asyncio.CancelledError` is a `BaseException`, not an `Exception`, catching
only `except Exception` around the resolving `await` looks correct but
silently skips settling the future on cancellation — confirmed by direct
reproduction, not assumed (see
`docs/infrastructure/latency-investigation.md`'s Docker/infra
investigation): any concurrent waiter coalescing onto that future then
hangs indefinitely, since nothing else was ever going to resolve it.
Fixed by catching `BaseException`, wrapping a bare `CancelledError` as a
plain `RuntimeError` before setting it on the future (so it reads as a
normal catchable failure to the waiter rather than bleeding an unrelated
cancellation into that waiter's own task), and still re-raising the
original error so the cancelled task's own cancellation semantics are
unaffected. Regression test:
`tests/test_parse_service.py::test_cancelling_the_resolving_request_does_not_hang_coalesced_waiters`.

**To swap the backing store** (Redis, Memcached), write a new class that
implements `CacheRepository` and change that one construction line. No
service or router changes.

### `OpenAIRepository` — OpenAI-specific, not provider-agnostic

`y2_ai_search_api/repositories/openai_repository.py` wraps `openai.AsyncOpenAI`. Unlike
a typical template's provider-agnostic `LlmRepository`, this class is named
and shaped for OpenAI specifically — see
[`../decisions/0002-openai-specific-repository.md`](../decisions/0002-openai-specific-repository.md)
for why that's a deliberate choice here, not an oversight.

It exposes `chat(...)` (returns the raw response — callers need
`.choices[0].logprobs` and `.usage`, not just the text) and `embed(...)`.
Both are `async def`, both check `settings.openai_api_key` up front and
raise `OpenAIUnavailableError` immediately rather than attempting a call
that would just fail — callers (`llm_fallback_service`) treat that
uniformly with any other API failure as an `api_error` outcome.

## Rules

- **Repositories own I/O; nothing else does.** Services and routers never
  call `AsyncOpenAI(...)` or read `data/taxonomy.json` directly.
- **Return plain data**, not transport types.
- **Keep the interface minimal.** Add a method when a caller needs it, not
  speculatively.
- **`log_event(...)` at real decision points inside repository methods**
  (a cache hit/miss, a model call's outcome) — see
  [logging.md](logging.md). Not a blanket per-method decorator.
- **Metrics get recorded here too, not upstream.** Token/cost counters are
  incremented inside `OpenAIRepository` right where `response.usage` is
  available, and the cache-hit/miss counter is incremented inside
  `CacheRepository.get` — co-locating the metric with the data it's
  computed from, rather than reconstructing it later from a result object.
