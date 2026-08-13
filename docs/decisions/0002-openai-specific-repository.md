# 0002 - OpenAI-specific repository, not a provider-agnostic abstraction

**Decision:** `app/repositories/openai_repository.py` defines
`OpenAIRepository`, a concrete class built on `openai.AsyncOpenAI` — not an
`LlmRepository` interface with a swappable, base-URL-configurable
implementation. This is a deliberate deviation from
`docs/conventions/repositories.md`'s general provider-agnostic pattern for
model clients. If multi-provider support is ever needed, the fix is to
introduce an `LlmRepository` interface with `OpenAIRepository` as one
implementation behind it — not to bolt `OPENAI_BASE_URL` configuration back
onto this class.

**Why:** The assignment brief pins the provider ("Use environment
variables/config for API keys" — for OpenAI specifically — with no
requirement to support alternative providers), and this service leans on
two OpenAI-specific capabilities that a base-URL-swappable abstraction
would have to either lose or awkwardly special-case: Structured Outputs
strict-mode JSON schemas (not universally supported the same way across
OpenAI-compatible endpoints) and per-token `logprobs` on chat completions
(used directly for the measured LLM-tier confidence score — see ADR 0001).
Naming the class `LlmRepository` while hard-coding both of these would be
dishonest — the name would promise provider flexibility the implementation
doesn't deliver. Calling it `OpenAIRepository` says exactly what it is.

**Satisfies:** "Use environment variables/config for API keys, never
hardcode secrets" (satisfied via `settings.openai_api_key`, `OPENAI_API_KEY`
in `.env` — see `docs/conventions/configuration.md`); code-quality
evaluation criterion (readability — the name matches the implementation).
