"""Zero-signal queries (classification.confidence == 0.0 -- zero taxonomy-
term/cue-word evidence for every vertical) must not silently default to
Vertical's first-declared member via max()'s tie-break. See
services.parse_service._resolve and services.llm_fallback_service.
run_category_classification.
"""

import json
from types import SimpleNamespace

import pytest

from repositories.cache_repository import cache_repository
from repositories.openai_repository import OpenAIRepository, OpenAIUnavailableError
from schema.taxonomy_models import Vertical
from services import llm_fallback_service, parse_service
from services.classifier_service import classify_query
from services.normalizer_service import normalize_query
from services.sanitizer_service import sanitize_query


@pytest.fixture(autouse=True)
def clear_cache_between_tests():
    cache_repository._cache.clear()
    yield


# The exact reproduction case: "ג'יפ" (jeep) isn't a taxonomy term or cue
# word, "20 אש''ח" (20 thousand NIS, doubled-ASCII-apostrophe gershayim)
# isn't recognized -- zero signal for every vertical, so classify_query's
# max()-tie-break "winner" used to be handed straight to the LLM fallback as
# if it were a real pick, defaulting to Vertical.REAL_ESTATE (declared
# first in the enum).
JEEP_QUERY = "ג'יפ קטן עד 20 אש''ח"


def test_repro_query_scores_exactly_zero_confidence_on_the_rule_path():
    canonical = normalize_query(sanitize_query(JEEP_QUERY))
    result = classify_query(canonical)
    assert result.confidence == 0.0
    # The defaulted "winner" is exactly the bug this test-suite is guarding
    # against being trusted -- documenting it, not asserting it's correct.
    assert result.vertical == Vertical.REAL_ESTATE


def _fake_chat_with_classification(category: str):
    async def fake_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None):
        schema_name = (response_format or {}).get("json_schema", {}).get("name")
        if schema_name == "query_category":
            content = json.dumps({"קטגוריה": category}, ensure_ascii=False)
        else:
            content = "{}"
        token_logprobs = [SimpleNamespace(token=character, logprob=-0.01) for character in content]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                    logprobs=SimpleNamespace(content=token_logprobs),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12),
        )

    return fake_chat


async def test_zero_signal_query_is_routed_through_classify_only_call_not_defaulted(monkeypatch):
    async def fake_embed(text, model=None):
        return [1.0, 0.0]

    monkeypatch.setattr(OpenAIRepository, "chat", staticmethod(_fake_chat_with_classification(Vertical.VEHICLES.value)))
    monkeypatch.setattr(OpenAIRepository, "embed", staticmethod(fake_embed))

    result = await parse_service.parse_query(JEEP_QUERY)

    assert result.response.category == Vertical.VEHICLES
    assert result.path == "llm"


async def test_classify_call_api_error_degrades_with_category_specific_note(monkeypatch):
    async def failing_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None):
        raise OpenAIUnavailableError("no key configured")

    monkeypatch.setattr(OpenAIRepository, "chat", staticmethod(failing_chat))

    result = await parse_service.parse_query(JEEP_QUERY)

    assert result.response.category == Vertical.REAL_ESTATE  # the honest, disclosed default
    assert result.response.confidence == llm_fallback_service.DEGRADED_CONFIDENCE
    assert llm_fallback_service.CATEGORY_DEGRADED_NOTE in result.response.notes


async def test_category_degraded_log_line_includes_confidence(monkeypatch):
    # Observability: the other two parse_decision log calls in _resolve
    # both carry a confidence value -- this one must too, not just an
    # outcome label with no number attached to it.
    logged_calls = []

    def fake_log_event(**fields):
        logged_calls.append(fields)

    async def failing_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None):
        raise OpenAIUnavailableError("no key configured")

    monkeypatch.setattr(parse_service, "log_event", fake_log_event)
    monkeypatch.setattr(OpenAIRepository, "chat", staticmethod(failing_chat))

    await parse_service.parse_query(JEEP_QUERY)

    decision_logs = [call for call in logged_calls if call.get("event") == "parse_decision"]
    assert len(decision_logs) == 1
    assert decision_logs[0]["outcome"] == "category_degraded"
    assert decision_logs[0]["confidence"] == llm_fallback_service.DEGRADED_CONFIDENCE
    assert decision_logs[0]["rule_path_confidence"] == 0.0


async def test_classify_call_malformed_response_degrades(monkeypatch):
    async def malformed_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None):
        content = "{}"  # missing the required קטגוריה field
        token_logprobs = [SimpleNamespace(token=character, logprob=-0.01) for character in content]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                    logprobs=SimpleNamespace(content=token_logprobs),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1, total_tokens=11),
        )

    monkeypatch.setattr(OpenAIRepository, "chat", staticmethod(malformed_chat))

    result = await parse_service.parse_query(JEEP_QUERY)

    assert result.response.confidence == llm_fallback_service.DEGRADED_CONFIDENCE
    assert llm_fallback_service.CATEGORY_DEGRADED_NOTE in result.response.notes


async def test_run_category_classification_returns_none_on_api_error(monkeypatch):
    async def failing_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None):
        raise OpenAIUnavailableError("boom")

    monkeypatch.setattr(OpenAIRepository, "chat", staticmethod(failing_chat))

    result = await llm_fallback_service.run_category_classification("ג'יפ קטן")
    assert result is None


async def test_run_category_classification_returns_vertical_on_success(monkeypatch):
    monkeypatch.setattr(
        OpenAIRepository, "chat", staticmethod(_fake_chat_with_classification(Vertical.USED_GOODS.value))
    )

    result = await llm_fallback_service.run_category_classification("משהו")
    assert result == Vertical.USED_GOODS


async def test_partial_signal_below_threshold_still_uses_rule_vertical_as_hint(monkeypatch):
    # 0 < confidence < threshold must NOT go through the classify-only path
    # -- classification.vertical is a real, partial signal here, not a
    # tie-break default, and the existing hint-into-fallback behavior stays.
    calls = {"classify": 0, "extract": 0}

    async def fake_chat(messages, model, response_format=None, logprobs=False, max_completion_tokens=None):
        schema_name = (response_format or {}).get("json_schema", {}).get("name")
        if schema_name == "query_category":
            calls["classify"] += 1
        else:
            calls["extract"] += 1
        content = "{}"
        token_logprobs = [SimpleNamespace(token=character, logprob=-0.01) for character in content]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                    logprobs=SimpleNamespace(content=token_logprobs),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1, total_tokens=11),
        )

    async def fake_embed(text, model=None):
        return [1.0, 0.0]

    monkeypatch.setattr(OpenAIRepository, "chat", staticmethod(fake_chat))
    monkeypatch.setattr(OpenAIRepository, "embed", staticmethod(fake_embed))

    result = await parse_service.parse_query("דירה בירושלים עד מיליון שח")

    assert calls["classify"] == 0
    assert calls["extract"] == 1
    assert result.response.category == Vertical.REAL_ESTATE
