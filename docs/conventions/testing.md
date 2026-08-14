# Testing

Tests should run anywhere, instantly, with no network and no API key.

```bash
cd api
uv run pytest
```

99 tests, `~2s`, zero network calls.

## What's covered (`api/tests/`)

| Test file | Asserts |
|---|---|
| `test_sanitizer_service.py` | control chars/emoji/zero-width/bidi stripped, oversized input truncated, empty rejected |
| `test_normalizer_service.py` | magnitude expansion, range rewriting, currency canonicalization, typo correction incl. prefix-stripping fallback |
| `test_classifier_service.py` | vertical detection + confidence bounds and behavior on golden/low-signal queries |
| `test_extractor_service.py` | full rule-path extraction against the golden example set in `docs/examples.md` |
| `test_cache_repository.py` | hit/miss, TTL expiry, taxonomy-version-keyed invalidation |
| `test_llm_confidence_service.py` | value-token span mapping isolates value uncertainty from JSON structure; embedding fallback behavior |
| `test_llm_fallback_service.py` | tier1 success / tier1-fail→tier2 success / both-fail→degrade / api_error at each tier, strict-schema shape |
| `test_parse_api.py` | `/parse` end-to-end incl. cache hit/miss, `/health`, `/metrics` |
| `test_parse_service.py` | in-flight request coalescing — N concurrent identical LLM-path queries hit `OpenAIRepository` exactly once; concurrent callers all see the same failure when the coalesced call errors |
| `test_auth.py` | open when `API_ACCESS_KEY` unset, 403 when set and wrong, open `/health`+`/metrics` regardless |
| `test_security_redteam.py` | prompt injection, unicode tricks (zero-width, bidi override, homoglyphs), oversized input, slang, unknown-field rejection |

## Mocking the model

`conftest.py`'s `mock_llm` fixture monkeypatches `OpenAIRepository.chat`/`.embed`
with **async** fakes (the repository is async — see
[llm-usage.md](llm-usage.md)):

```python
async def test_something(client, mock_llm):
    response = client.post("/parse", json={"q": "..."})
```

Individual tier-cascade tests (`test_llm_fallback_service.py`) monkeypatch
`OpenAIRepository.chat` directly with a per-test async fake to control
exactly which tier succeeds/fails.

Rules:
- **Never call a real provider in tests.** Use `mock_llm`, or a per-test
  monkeypatch of `OpenAIRepository`.
- **Toggle auth with env vars** (`monkeypatch.setenv("API_ACCESS_KEY", ...)`),
  since `verify_api_key` reads the key per request.
- **Cache state leaks across tests** — `cache_repository` is a module-level
  singleton. `test_parse_api.py` uses an `autouse` fixture that clears
  `cache_repository._cache` before every test in that file; add a query to
  an existing test file's fixture scope rather than a fresh singleton per test.
- Add a test when you add an endpoint, a taxonomy field-extraction pattern,
  or a branch worth protecting (especially in the tier-escalation logic).

## Async tests

`pyproject.toml` sets `asyncio_mode = "auto"` — an `async def test_...`
function is automatically treated as an async test, no
`@pytest.mark.asyncio` decorator needed. Async mocks must return
awaitables: `async def fake_chat(...): return ...`, not a plain function.

## The one thing tests can't cover

Real OpenAI API behavior — real model outputs, real latency, real
Structured-Outputs schema acceptance. That's what
`docker compose up` + a real `curl` against a running container with
`OPENAI_API_KEY` set is for, and what `scripts/loadtest.py` measures under
concurrent load. Both are part of this project's actual verification
record — see the root `README.md`'s verification column — not just unit
tests in isolation.
