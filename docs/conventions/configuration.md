# Configuration

All configuration is environment-driven through a single typed settings
object, with one deliberate exception (below) — not scattered `os.getenv`
calls across the codebase.

## `app/config.py`

```python
class Settings(BaseSettings):
    log_level: str = "INFO"
    api_access_key: str = ""
    openai_api_key: str = ""
    openai_fallback_model: str = "gpt-4.1-nano"
    openai_escalation_model: str = "gpt-4.1-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    confidence_threshold: float = 0.58
    cache_ttl_seconds: int = 3600
    cache_max_size: int = 10000
    max_query_length: int = 500
    project_name: str = "Yad2 Search-Understanding Service"
    version: str = "0.1.0"

settings = Settings()
```

Rules:
- One `Settings` (`pydantic_settings.BaseSettings`). Import `settings`
  where you need config.
- Every setting has a **safe default** so the app boots with an empty `.env`.
- `.env.example` is the documented contract. Copy it to `.env` to run.

## The env vars

| Variable | Meaning |
|---|---|
| `LOG_LEVEL` | `INFO` / `DEBUG` |
| `API_ACCESS_KEY` | If set, `/parse` requires `X-API-Key`. **Empty = open** |
| `OPENAI_API_KEY` | Required for the LLM-fallback tier and the embedding confidence cross-check. **Empty = rule path only, LLM fallback degrades gracefully** |
| `OPENAI_FALLBACK_MODEL` | Tier 1 model (cheapest suitable, default `gpt-4.1-nano`) |
| `OPENAI_ESCALATION_MODEL` | Tier 2 model (only called if Tier 1 fails validation, default `gpt-4.1-mini`) |
| `OPENAI_EMBEDDING_MODEL` | Used only for the LLM-tier confidence cross-check |
| `CONFIDENCE_THRESHOLD` | Rule-path confidence at/above this skips the LLM fallback entirely |
| `CACHE_TTL_SECONDS` / `CACHE_MAX_SIZE` | Full-response cache bounds |
| `MAX_QUERY_LENGTH` | Sanitizer's soft truncation cap (the request schema also hard-rejects anything over 50,000 characters, before sanitization even runs) |

## The one deliberate `os.getenv` exception

`config.get_api_access_key()` reads `API_ACCESS_KEY` via `os.getenv` at call
time instead of `settings.api_access_key`. Every other setting is read once
at process start and never needs to change mid-run; this one does —
`test_auth.py` toggles `API_ACCESS_KEY` per test via `monkeypatch.setenv`,
and `settings` is a module-level singleton built once at import, so reading
`settings.api_access_key` here would freeze whatever value was present at
process start. `verify_api_key` calls this function per-request specifically
so auth can be enabled/disabled live. See the function's docstring in
`app/config.py` for the full rationale.

## Secrets

- Secrets come from the environment only. Never commit a real `.env` — only
  `.env.example` is tracked (see [`.gitignore`](../../.gitignore)).
- Don't pass secrets as function arguments (the logger truncates but
  doesn't redact — see [logging.md](logging.md)). `OPENAI_API_KEY` is read
  directly from `settings` inside `OpenAIRepository`, never threaded
  through a function call.

## Auth is optional locally

`API_ACCESS_KEY` empty means `verify_api_key` is a no-op and `/parse` is
open — frictionless local dev and grading. `/health` and `/metrics` are
always open regardless, since a liveness probe or a scraper generally can't
send a custom header.

## The LLM fallback degrades, it doesn't require a key

Unlike `API_ACCESS_KEY`, an unset `OPENAI_API_KEY` doesn't disable a
feature outright — it changes what happens on the (already rare,
low-confidence) fallback path: both tiers short-circuit to an `api_error`
outcome, and the pipeline degrades to the rule path's own result rather
than failing the request. See
[`../decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md`](../decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md).
