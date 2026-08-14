import os

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Settings(BaseSettings):
    """Application configuration, loaded from environment / .env.

    Only neutral, non-secret settings are ever returned by the public API.
    """

    # Logging
    log_level: str = "INFO"
    max_str_log_length: int = 200

    # Auth — when empty, API key auth is a no-op (open, for local dev/grading)
    api_access_key: str = ""

    # OpenAI — this service is OpenAI-specific by design, not a
    # swappable-provider abstraction.
    openai_api_key: str = ""
    # gpt-5-nano/gpt-5-mini were the original picks by price, but OpenAI
    # rejects logprobs requests on the entire gpt-5 family (verified live,
    # 403 "not allowed to request logprobs from this model") — and this
    # service's confidence score depends on logprobs. gpt-4.1-nano/mini are
    # the cheapest models confirmed (live) to support both Structured
    # Outputs strict mode and logprobs together. See docs/DESIGN.md.
    openai_fallback_model: str = "gpt-4.1-nano"
    openai_escalation_model: str = "gpt-4.1-mini"
    openai_embedding_model: str = "text-embedding-3-small"

    # Rule-path confidence at or above this value skips the LLM fallback
    # entirely. Tuned against the golden example set in docs/examples.md —
    # see docs/DESIGN.md's Confidence methodology section.
    confidence_threshold: float = 0.58

    # Full-response cache (normalized-query -> ParseResponse)
    cache_ttl_seconds: int = 3600
    cache_max_size: int = 10000

    # Input sanitization
    max_query_length: int = 500

    # Service metadata
    project_name: str = "Yad2 Search-Understanding Service"
    version: str = "0.1.0"

    model_config = SettingsConfigDict(extra="ignore")


settings = Settings()


def get_api_access_key() -> str:
    """Read the API access key at call time so tests / runtime can toggle it.

    Returns an empty string when unset, which the security layer treats as
    "auth disabled" (open access) for local development.

    Deliberately still `os.getenv`, not `settings.api_access_key` — the
    logging cleanup routed LOG_LEVEL/MAX_STR_LOG_LENGTH through Settings
    precisely because those are set once at process start and never need
    to change mid-run. This one does: `test_auth.py` toggles
    `API_ACCESS_KEY` per test via `monkeypatch.setenv(...)`, and `settings`
    is a module-level singleton built once at import — reading
    `settings.api_access_key` here would freeze whatever value was present
    when the process started, silently breaking every test that flips the
    key at runtime. `verify_api_key` calls this function per-request
    specifically so auth can be enabled/disabled live, which is the whole
    point of `API_ACCESS_KEY` empty = open for local dev.
    """
    return os.getenv("API_ACCESS_KEY", settings.api_access_key) or ""
