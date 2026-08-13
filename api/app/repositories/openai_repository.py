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
from app.logger import log_activity, log_metric
from app.metrics import record_token_usage_and_cost

Message = dict[str, str]


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
            cls._client = AsyncOpenAI(api_key=settings.openai_api_key)
        return cls._client

    @classmethod
    @log_activity
    async def chat(
        cls,
        messages: list[Message],
        model: str,
        response_format: dict | None = None,
        logprobs: bool = False,
    ):
        """Send a chat completion request and return the raw response object
        (not just the text) — callers need ``.choices[0].logprobs`` for the
        confidence score and ``.usage`` for cost tracking, not only the
        completion text.
        """
        if not settings.openai_api_key:
            log_metric(event="llm_call_outcome", outcome="api_error", reason="missing_api_key", model=model)
            raise OpenAIUnavailableError("OPENAI_API_KEY is not configured")

        request_kwargs: dict = {"model": model, "messages": messages}
        if response_format is not None:
            request_kwargs["response_format"] = response_format
        if logprobs:
            request_kwargs["logprobs"] = True

        try:
            response = await cls._get_client().chat.completions.create(**request_kwargs)
        except Exception as error:
            log_metric(event="llm_call_outcome", outcome="api_error", reason=str(error), model=model)
            raise OpenAIUnavailableError(str(error)) from error

        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        log_metric(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=usage.total_tokens if usage else 0,
        )
        record_token_usage_and_cost(model, prompt_tokens, completion_tokens)
        return response

    @classmethod
    @log_activity
    async def embed(cls, text: str, model: str | None = None) -> list[float]:
        model = model or settings.openai_embedding_model

        if not settings.openai_api_key:
            log_metric(event="llm_call_outcome", outcome="api_error", reason="missing_api_key", model=model)
            raise OpenAIUnavailableError("OPENAI_API_KEY is not configured")

        try:
            response = await cls._get_client().embeddings.create(input=text, model=model)
        except Exception as error:
            log_metric(event="llm_call_outcome", outcome="api_error", reason=str(error), model=model)
            raise OpenAIUnavailableError(str(error)) from error

        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        log_metric(model=model, prompt_tokens=prompt_tokens, total_tokens=usage.total_tokens if usage else 0)
        record_token_usage_and_cost(model, prompt_tokens, completion_tokens=0)
        return response.data[0].embedding
