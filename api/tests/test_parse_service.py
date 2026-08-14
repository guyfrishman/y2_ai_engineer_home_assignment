import pytest

from app import metrics
from app.repositories.cache_repository import cache_repository
from app.services import parse_service


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


async def test_concurrent_identical_llm_queries_coalesce_into_one_api_call(monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace

    from app.repositories.openai_repository import OpenAIRepository

    call_count = {"n": 0}

    async def fake_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None, reasoning_effort=None):
        call_count["n"] += 1
        # A real network call would yield control here too -- sleeping is
        # what lets the other concurrently-gathered requests actually
        # arrive while this one is still in flight, exercising the
        # coalescing path instead of resolving one at a time.
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

    # Rule confidence 0.5 < confidence_threshold (0.58) -> every one of
    # these identical concurrent requests would trigger the LLM fallback
    # if not coalesced.
    query = "דירת 3 חדרים בירושלים עד מליון שח"
    concurrency = 10
    results = await asyncio.gather(*(parse_service.parse_query(query) for _ in range(concurrency)))

    assert call_count["n"] == 1, "N concurrent identical requests should trigger exactly one OpenAI call"

    paths = [result.path for result in results]
    assert paths.count("llm") == 1
    assert paths.count("coalesced") == concurrency - 1

    # Every caller gets the same resolved answer, not just the "owner".
    first_response = results[0].response
    for result in results:
        assert result.response.params == first_response.params
        assert result.response.confidence == first_response.confidence


async def test_concurrent_identical_queries_all_see_the_same_failure(monkeypatch):
    import asyncio
    import gc
    import warnings

    async def failing_resolve(canonical_query, cache_key):
        await asyncio.sleep(0.05)
        raise RuntimeError("simulated failure during resolution")

    monkeypatch.setattr(parse_service, "_resolve", failing_resolve)

    query = "דירת 3 חדרים בירושלים עד מליון שח"
    with warnings.catch_warnings():
        # An asyncio.Future whose exception is set but never retrieved
        # logs "exception was never retrieved" on GC -- promote that to a
        # real failure so a coalescing bug (owner's exception not properly
        # marked retrieved) doesn't silently pass this test.
        warnings.simplefilter("error")
        results = await asyncio.gather(
            *(parse_service.parse_query(query) for _ in range(5)), return_exceptions=True
        )
        gc.collect()

    assert len(results) == 5
    assert all(isinstance(result, RuntimeError) for result in results)
    assert all("simulated failure during resolution" in str(result) for result in results)


async def test_cancelling_the_resolving_request_does_not_hang_coalesced_waiters(monkeypatch):
    import asyncio

    # Simulates a graceful shutdown: uvicorn cancels the owning request's
    # task once the grace period elapses while it's still mid-resolution.
    resolving_started = asyncio.Event()
    block_forever = asyncio.Event()

    async def hanging_resolve(canonical_query, cache_key):
        resolving_started.set()
        await block_forever.wait()  # never set -- this coroutine only ever ends via cancellation
        raise AssertionError("unreachable")

    monkeypatch.setattr(parse_service, "_resolve", hanging_resolve)

    query = "דירת 3 חדרים בירושלים עד מליון שח"
    owner_task = asyncio.create_task(parse_service.parse_query(query))
    await asyncio.wait_for(resolving_started.wait(), timeout=1.0)

    waiter_task = asyncio.create_task(parse_service.parse_query(query))
    await asyncio.sleep(0.05)  # let the waiter reach `await existing_future` before cancelling the owner

    owner_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner_task

    # The real assertion: cancelling the owner must not leave the
    # coalesced waiter hanging forever -- it should fail promptly with a
    # normal, catchable exception instead of a bare CancelledError bleeding
    # into an unrelated task. A 1s bound here is a test-safety net, not the
    # expected latency -- a fixed implementation settles this in microseconds.
    with pytest.raises(RuntimeError, match="in-flight resolution did not complete"):
        await asyncio.wait_for(waiter_task, timeout=1.0)
