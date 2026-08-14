import time

import httpx2
from openai import AsyncOpenAI

from config import settings
from logger import log_event
from metrics import record_token_usage_and_cost

Message = dict[str, str]
_OPENAI_CONNECTION_LIMITS = httpx2.Limits(max_connections=2000, max_keepalive_connections=200)


class OpenAIUnavailableError(RuntimeError):
    """Raised whenever a call to OpenAI cannot be attempted or fails:
    missing API key, network error, rate limit, provider outage, etc."""


class OpenAIRepository:
    _client: AsyncOpenAI | None = None

    @classmethod
    def _get_client(cls) -> AsyncOpenAI:
        if cls._client is None:
            cls._client = AsyncOpenAI(
                api_key=settings.openai_api_key,
                timeout=settings.openai_request_timeout_seconds,
                max_retries=settings.openai_max_retries,
                http_client=httpx2.AsyncClient(limits=_OPENAI_CONNECTION_LIMITS),
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
        reasoning_effort: str | None = None,
    ):

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
        if reasoning_effort is not None:
            request_kwargs["reasoning_effort"] = reasoning_effort

        started_at = time.perf_counter()
        try:
            response = await cls._get_client().chat.completions.create(**request_kwargs)
        except Exception as error:
            log_event(
                event="llm_call_outcome",
                outcome="api_error",
                reason=str(error),
                model=model,
                duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
            )
            raise OpenAIUnavailableError(str(error)) from error

        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        log_event(
            event="llm_call_outcome",
            outcome="success",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=usage.total_tokens if usage else 0,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
        )
        record_token_usage_and_cost(model, prompt_tokens, completion_tokens)
        return response

    @classmethod
    async def embed(cls, text: str, model: str | None = None) -> list[float]:
        model = model or settings.openai_embedding_model

        if not settings.openai_api_key:
            log_event(event="llm_call_outcome", outcome="api_error", reason="missing_api_key", model=model)
            raise OpenAIUnavailableError("OPENAI_API_KEY is not configured")

        started_at = time.perf_counter()
        try:
            response = await cls._get_client().embeddings.create(input=text, model=model)
        except Exception as error:
            log_event(
                event="llm_call_outcome",
                outcome="api_error",
                reason=str(error),
                model=model,
                duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
            )
            raise OpenAIUnavailableError(str(error)) from error

        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        log_event(
            event="llm_call_outcome",
            outcome="success",
            model=model,
            prompt_tokens=prompt_tokens,
            total_tokens=usage.total_tokens if usage else 0,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
        )
        record_token_usage_and_cost(model, prompt_tokens, completion_tokens=0)
        return response.data[0].embedding
