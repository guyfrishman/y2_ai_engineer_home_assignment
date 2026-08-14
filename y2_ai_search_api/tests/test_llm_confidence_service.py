import json
import re
from types import SimpleNamespace

from services import llm_confidence_service
from services.llm_confidence_service import (
    compute_embedding_similarity,
    compute_llm_confidence,
    compute_logprob_confidence,
)


def _fabricate_tokens(json_text: str, uncertain_substrings: list[str], uncertain_logprob=-2.0, certain_logprob=-0.01):
    uncertain_positions = set()
    for substring in uncertain_substrings:
        for match in re.finditer(re.escape(substring), json_text):
            uncertain_positions.update(range(match.start(), match.end()))
    tokens = []
    for index, character in enumerate(json_text):
        logprob = uncertain_logprob if index in uncertain_positions else certain_logprob
        tokens.append(SimpleNamespace(token=character, logprob=logprob))
    return tokens


def test_confidence_reflects_value_token_uncertainty_not_structure():
    json_text = json.dumps({"מחיר": {"max": 70000}, "צבע": "לבן"}, ensure_ascii=False)
    confident_tokens = _fabricate_tokens(json_text, uncertain_substrings=[])
    uncertain_value_tokens = _fabricate_tokens(json_text, uncertain_substrings=["70000", "לבן"])

    high = compute_logprob_confidence(confident_tokens, ["מחיר", "צבע"])
    low = compute_logprob_confidence(uncertain_value_tokens, ["מחיר", "צבע"])

    assert low < high
    assert 0.0 <= low <= 1.0
    assert 0.0 <= high <= 1.0


def test_empty_token_list_returns_zero():
    assert compute_logprob_confidence([], ["מחיר"]) == 0.0


def test_no_matching_value_spans_falls_back_to_averaging_all_tokens():
    json_text = json.dumps({"מחיר": 100}, ensure_ascii=False)
    tokens = _fabricate_tokens(json_text, uncertain_substrings=[])
    # Field name not present in the JSON at all -> no value spans found.
    confidence = compute_logprob_confidence(tokens, ["שדה_לא_קיים"])
    assert confidence > 0.0


async def test_embedding_similarity_uses_openai_repository_embed(monkeypatch):
    vectors = {"call_count": 0}

    async def fake_embed(text, model=None):
        vectors["call_count"] += 1
        return [1.0, 0.0]

    monkeypatch.setattr(llm_confidence_service.OpenAIRepository, "embed", staticmethod(fake_embed))

    similarity = await compute_embedding_similarity("דירה בירושלים", {"עיר": "ירושלים"})
    assert similarity == 1.0
    assert vectors["call_count"] == 2


async def test_embedding_similarity_returns_zero_for_empty_params():
    assert await compute_embedding_similarity("דירה בירושלים", {}) == 0.0


async def test_compute_llm_confidence_falls_back_to_logprob_only_when_embedding_unavailable(monkeypatch):
    from repositories.openai_repository import OpenAIUnavailableError

    async def failing_embed(text, model=None):
        raise OpenAIUnavailableError("no key")

    monkeypatch.setattr(llm_confidence_service.OpenAIRepository, "embed", staticmethod(failing_embed))

    # certain_logprob=-0.3 -> exp(-0.3)=~0.74, deliberately mid-range (not
    # decisive) so this exercises the embedding-call-then-fallback path,
    # not the skip-when-decisive short-circuit tested separately below.
    json_text = json.dumps({"מחיר": 100}, ensure_ascii=False)
    tokens = _fabricate_tokens(json_text, uncertain_substrings=[], certain_logprob=-0.3)
    confidence = await compute_llm_confidence("דירה", tokens, ["מחיר"], {"מחיר": 100})
    logprob_only = compute_logprob_confidence(tokens, ["מחיר"])
    assert llm_confidence_service.DECISIVE_LOW_THRESHOLD < logprob_only < llm_confidence_service.DECISIVE_HIGH_THRESHOLD
    assert confidence == round(logprob_only, 10) or abs(confidence - logprob_only) < 1e-9


async def test_confidence_is_clamped_to_unit_interval(monkeypatch):
    async def fake_embed(text, model=None):
        return [1.0, 0.0]

    monkeypatch.setattr(llm_confidence_service.OpenAIRepository, "embed", staticmethod(fake_embed))
    # Mid-range logprob (not decisive), so blending actually runs.
    json_text = json.dumps({"מחיר": 100}, ensure_ascii=False)
    tokens = _fabricate_tokens(json_text, uncertain_substrings=[], certain_logprob=-0.3)
    confidence = await compute_llm_confidence("דירה", tokens, ["מחיר"], {"מחיר": 100})
    assert 0.0 <= confidence <= 1.0


async def test_decisive_high_logprob_skips_embedding_call(monkeypatch):
    call_count = {"n": 0}

    async def fake_embed(text, model=None):
        call_count["n"] += 1
        return [1.0, 0.0]

    monkeypatch.setattr(llm_confidence_service.OpenAIRepository, "embed", staticmethod(fake_embed))
    # certain_logprob=-0.01 -> exp(-0.01)=~0.99, comfortably above
    # DECISIVE_HIGH_THRESHOLD.
    json_text = json.dumps({"מחיר": 100}, ensure_ascii=False)
    tokens = _fabricate_tokens(json_text, uncertain_substrings=[], certain_logprob=-0.01)
    logprob_only = compute_logprob_confidence(tokens, ["מחיר"])
    assert logprob_only >= llm_confidence_service.DECISIVE_HIGH_THRESHOLD

    confidence = await compute_llm_confidence("דירה", tokens, ["מחיר"], {"מחיר": 100})
    assert confidence == logprob_only
    assert call_count["n"] == 0  # embedding never called -- skipped as decisive


async def test_decisive_low_logprob_skips_embedding_call(monkeypatch):
    call_count = {"n": 0}

    async def fake_embed(text, model=None):
        call_count["n"] += 1
        return [1.0, 0.0]

    monkeypatch.setattr(llm_confidence_service.OpenAIRepository, "embed", staticmethod(fake_embed))
    # certain_logprob=-5.0 -> exp(-5.0)=~0.0067, comfortably below
    # DECISIVE_LOW_THRESHOLD.
    json_text = json.dumps({"מחיר": 100}, ensure_ascii=False)
    tokens = _fabricate_tokens(json_text, uncertain_substrings=[], certain_logprob=-5.0)
    logprob_only = compute_logprob_confidence(tokens, ["מחיר"])
    assert logprob_only <= llm_confidence_service.DECISIVE_LOW_THRESHOLD

    confidence = await compute_llm_confidence("דירה", tokens, ["מחיר"], {"מחיר": 100})
    assert confidence == logprob_only
    assert call_count["n"] == 0
