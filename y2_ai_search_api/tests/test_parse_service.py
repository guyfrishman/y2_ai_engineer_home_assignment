import pytest

import metrics
from repositories.cache_repository import cache_repository
from services import parse_service


@pytest.fixture(autouse=True)
def clear_cache_between_tests():
    cache_repository._cache.clear()
    yield


def _counter_value() -> float:
    return metrics.PARSE_ERRORS_TOTAL._value.get()


async def test_exception_mid_pipeline_increments_error_counter_and_still_propagates(monkeypatch):
    before = _counter_value()

    def boom(canonical_query):
        raise RuntimeError("simulated classifier failure")

    monkeypatch.setattr(parse_service, "classify_query", boom)

    with pytest.raises(RuntimeError, match="simulated classifier failure"):
        await parse_service.parse_query("דירה בירושלים")

    assert _counter_value() == before + 1


async def test_successful_request_does_not_increment_error_counter():
    before = _counter_value()
    await parse_service.parse_query("טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן")
    assert _counter_value() == before


async def test_concurrent_identical_queries_each_resolve_independently(monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace

    from repositories.openai_repository import OpenAIRepository

    call_count = {"n": 0}

    async def fake_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None, reasoning_effort=None):
        call_count["n"] += 1
        await asyncio.sleep(0.05)
        content = json.dumps({"עיר": "ירושלים"}, ensure_ascii=False)
        token_logprobs = [SimpleNamespace(token=character, logprob=-0.01) for character in content]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                    logprobs=SimpleNamespace(content=token_logprobs),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def fake_embed(text, model=None):
        return [1.0, 0.0]

    monkeypatch.setattr(OpenAIRepository, "chat", staticmethod(fake_chat))
    monkeypatch.setattr(OpenAIRepository, "embed", staticmethod(fake_embed))

    # No coalescing: N concurrent identical requests each pay their own
    # LLM call now, not sharing one.
    query = "דירה בירושלים עד מיליון שח"
    concurrency = 10
    results = await asyncio.gather(*(parse_service.parse_query(query) for _ in range(concurrency)))

    assert call_count["n"] == concurrency
    assert all(result.path == "llm" for result in results)
    first_response = results[0].response
    for result in results:
        assert result.response.params == first_response.params
