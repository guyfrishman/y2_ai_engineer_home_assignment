"""OpenAI-specific client — not a provider-agnostic abstraction. This
service only ever calls OpenAI (the brief pins the provider), so the class
is named and shaped for that rather than carrying a base-URL-swappable
LlmRepository interface it would never use. See
docs/decisions/0002-openai-specific-repository.md.

Async client, not sync: the LLM-fallback path is the only part of this
service with real network I/O, and it must not block the event loop while
waiting on it — a single slow model call would otherwise stall every other
concurrent request the single-worker Uvicorn process is handling. The
cheap, CPU-bound rule/cache path stays plain synchronous code (Python's GIL
means threading it would add overhead with no real parallelism); only this
network-bound edge needs asyncio.
"""

from openai import AsyncOpenAI

from app.config import settings
from app.logger import log_event
from app.metrics import record_token_usage_and_cost

Message = dict[str, str]

# The SDK's own defaults are max_retries=2, timeout=600s — meaning one
# "call" as measured anywhere else in this codebase could silently be up
# to three sequential attempts with backoff, and a hung request could block
# for ten minutes. This service already has its own retry-equivalent (the
# Tier 1 -> Tier 2 -> degrade cascade), so the SDK retrying underneath that
# just makes tier failures slower to observe, not more reliable. Explicit
# and short instead.
OPENAI_REQUEST_TIMEOUT_SECONDS = 5.0
OPENAI_MAX_RETRIES = 0


class OpenAIUnavailableError(RuntimeError):
    """Raised whenever a call to OpenAI cannot be attempted or fails —
    missing API key, network error, rate limit, provider outage, etc.
    Callers treat all of these uniformly as an ``api_error`` outcome and
    fall back per the tier-escalation policy in ``llm_fallback_service``."""


class OpenAIRepository:
    _client: AsyncOpenAI | None = None

    @classmethod
    def _get_client(cls) -> AsyncOpenAI:
        if cls._client is None:
            cls._client = AsyncOpenAI(
                api_key=settings.openai_api_key,
                timeout=OPENAI_REQUEST_TIMEOUT_SECONDS,
                max_retries=OPENAI_MAX_RETRIES,
            )
        return cls._client

    @classmethod
    async def chat(
        cls,
        messages: list[Message],
        model: str,
        response_format: dict | None = None,
        logprobs: bool = False,
        max_completion_tokens: int | None = None,
    ):
        """Send a chat completion request and return the raw response object
        (not just the text) — callers need ``.choices[0].logprobs`` for the
        confidence score and ``.usage`` for cost tracking, not only the
        completion text.
        """
        if not settings.openai_api_key:
            log_event(event="llm_call_outcome", outcome="api_error", reason="missing_api_key", model=model)
            raise OpenAIUnavailableError("OPENAI_API_KEY is not configured")

        request_kwargs: dict = {"model": model, "messages": messages}
        if response_format is not None:
            request_kwargs["response_format"] = response_format
        if logprobs:
            request_kwargs["logprobs"] = True
        if max_completion_tokens is not None:
            request_kwargs["max_completion_tokens"] = max_completion_tokens

        try:
            response = await cls._get_client().chat.completions.create(**request_kwargs)
        except Exception as error:
            log_event(event="llm_call_outcome", outcome="api_error", reason=str(error), model=model)
            raise OpenAIUnavailableError(str(error)) from error

        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        log_event(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=usage.total_tokens if usage else 0,
        )
        record_token_usage_and_cost(model, prompt_tokens, completion_tokens)
        return response

    @classmethod
    async def embed(cls, text: str, model: str | None = None) -> list[float]:
        model = model or settings.openai_embedding_model

        if not settings.openai_api_key:
            log_event(event="llm_call_outcome", outcome="api_error", reason="missing_api_key", model=model)
            raise OpenAIUnavailableError("OPENAI_API_KEY is not configured")

        try:
            response = await cls._get_client().embeddings.create(input=text, model=model)
        except Exception as error:
            log_event(event="llm_call_outcome", outcome="api_error", reason=str(error), model=model)
            raise OpenAIUnavailableError(str(error)) from error

        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        log_event(model=model, prompt_tokens=prompt_tokens, total_tokens=usage.total_tokens if usage else 0)
        record_token_usage_and_cost(model, prompt_tokens, completion_tokens=0)
        return response.data[0].embedding
