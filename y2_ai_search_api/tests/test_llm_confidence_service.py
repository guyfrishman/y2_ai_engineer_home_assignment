import json
import re
from types import SimpleNamespace

from schema.taxonomy_models import Vertical
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

    similarity = await compute_embedding_similarity("דירה בירושלים", {"עיר": "ירושלים"}, Vertical.REAL_ESTATE)
    assert similarity == 1.0
    assert vectors["call_count"] == 2


async def test_embedding_similarity_returns_zero_for_empty_params():
    # No fields extracted at all -- nothing to compare, no API calls made
    # (the category-prefix change doesn't turn an empty extraction into a
    # non-empty synthetic sentence).
    assert await compute_embedding_similarity("דירה בירושלים", {}, Vertical.REAL_ESTATE) == 0.0


async def test_embedding_similarity_sentence_is_prefixed_with_the_category(monkeypatch):
    captured_sentences = []

    async def fake_embed(text, model=None):
        captured_sentences.append(text)
        return [1.0, 0.0]

    monkeypatch.setattr(llm_confidence_service.OpenAIRepository, "embed", staticmethod(fake_embed))

    await compute_embedding_similarity("דירה בירושלים", {"עיר": "ירושלים"}, Vertical.REAL_ESTATE)

    # First call embeds the query itself (unprefixed); second embeds the
    # synthetic params sentence, which must carry the category.
    assert captured_sentences[0] == "דירה בירושלים"
    assert captured_sentences[1].startswith("קטגוריה: נדל״ן")


async def test_category_prefix_lowers_similarity_for_a_mismatched_category(monkeypatch):
    # Same extracted params, only the category differs -- an out-of-domain
    # extraction (e.g. a car query force-extracted under נדל״ן) should read
    # as less similar to the query than the correct category would, even
    # when the field values alone happen to look plausible either way.
    async def fake_embed(text, model=None):
        # A crude but deterministic stand-in for a real embedding: treat
        # each Hebrew "word" in the category tag as a one-hot dimension, so
        # a matching category cosine-aligns and a mismatched one doesn't.
        vertical_names = [v.value for v in Vertical]
        return [1.0 if name in text else 0.0 for name in vertical_names]

    monkeypatch.setattr(llm_confidence_service.OpenAIRepository, "embed", staticmethod(fake_embed))

    async def similarity_for(vertical: Vertical) -> float:
        return await compute_embedding_similarity(f"דירה {Vertical.REAL_ESTATE.value}", {"מחיר": 100}, vertical)

    matching = await similarity_for(Vertical.REAL_ESTATE)
    mismatched = await similarity_for(Vertical.VEHICLES)
    assert matching > mismatched


async def test_compute_llm_confidence_falls_back_to_logprob_only_when_embedding_unavailable(monkeypatch):
    from repositories.openai_repository import OpenAIUnavailableError

    async def failing_embed(text, model=None):
        raise OpenAIUnavailableError("no key")

    monkeypatch.setattr(llm_confidence_service.OpenAIRepository, "embed", staticmethod(failing_embed))

    json_text = json.dumps({"מחיר": 100}, ensure_ascii=False)
    tokens = _fabricate_tokens(json_text, uncertain_substrings=[], certain_logprob=-0.3)
    confidence = await compute_llm_confidence("דירה", tokens, ["מחיר"], {"מחיר": 100}, Vertical.REAL_ESTATE)
    logprob_only = compute_logprob_confidence(tokens, ["מחיר"])
    assert confidence == round(logprob_only, 10) or abs(confidence - logprob_only) < 1e-9


async def test_confidence_is_clamped_to_unit_interval(monkeypatch):
    async def fake_embed(text, model=None):
        return [1.0, 0.0]

    monkeypatch.setattr(llm_confidence_service.OpenAIRepository, "embed", staticmethod(fake_embed))
    json_text = json.dumps({"מחיר": 100}, ensure_ascii=False)
    tokens = _fabricate_tokens(json_text, uncertain_substrings=[], certain_logprob=-0.3)
    confidence = await compute_llm_confidence("דירה", tokens, ["מחיר"], {"מחיר": 100}, Vertical.REAL_ESTATE)
    assert 0.0 <= confidence <= 1.0


async def test_embedding_cross_check_always_runs_regardless_of_logprob_decisiveness(monkeypatch):
    # No more decisive-threshold skip: a model can be very confident about
    # tokens it typed while being categorically wrong about what it should
    # have been asked at all, so the embedding cross-check now runs on
    # every LLM-fallback response, not just the ones a logprob heuristic
    # judged borderline.
    call_count = {"n": 0}

    async def fake_embed(text, model=None):
        call_count["n"] += 1
        return [1.0, 0.0]

    monkeypatch.setattr(llm_confidence_service.OpenAIRepository, "embed", staticmethod(fake_embed))

    for certain_logprob in (-0.01, -5.0):  # what used to be "decisively high" and "decisively low"
        call_count["n"] = 0
        json_text = json.dumps({"מחיר": 100}, ensure_ascii=False)
        tokens = _fabricate_tokens(json_text, uncertain_substrings=[], certain_logprob=certain_logprob)
        await compute_llm_confidence("דירה", tokens, ["מחיר"], {"מחיר": 100}, Vertical.REAL_ESTATE)
        assert call_count["n"] == 2, f"embedding should always run (certain_logprob={certain_logprob})"


async def test_confidence_computed_logs_both_component_signals(monkeypatch):
    # Observability: the blended "confidence" number alone doesn't say
    # whether it came from the model's own token uncertainty, a semantic
    # mismatch the embedding cross-check caught, or both -- both components
    # must be visible in the logs, not just the final blend.
    logged = {}

    def fake_log_event(**fields):
        logged.update(fields)

    async def fake_embed(text, model=None):
        return [1.0, 0.0]

    monkeypatch.setattr(llm_confidence_service, "log_event", fake_log_event)
    monkeypatch.setattr(llm_confidence_service.OpenAIRepository, "embed", staticmethod(fake_embed))

    json_text = json.dumps({"מחיר": 100}, ensure_ascii=False)
    tokens = _fabricate_tokens(json_text, uncertain_substrings=[], certain_logprob=-0.3)
    confidence = await compute_llm_confidence("דירה", tokens, ["מחיר"], {"מחיר": 100}, Vertical.REAL_ESTATE)

    assert logged["event"] == "confidence_computed"
    assert logged["vertical"] == Vertical.REAL_ESTATE.value
    assert logged["confidence"] == round(confidence, 4)
    assert "logprob_confidence" in logged
    assert "embedding_similarity" in logged
    assert logged["embedding_outcome"] == "success"


async def test_embedding_unavailable_fallback_is_logged(monkeypatch):
    from repositories.openai_repository import OpenAIUnavailableError

    logged_events = []

    def fake_log_event(**fields):
        logged_events.append(fields)

    async def failing_embed(text, model=None):
        raise OpenAIUnavailableError("no key")

    monkeypatch.setattr(llm_confidence_service, "log_event", fake_log_event)
    monkeypatch.setattr(llm_confidence_service.OpenAIRepository, "embed", staticmethod(failing_embed))

    json_text = json.dumps({"מחיר": 100}, ensure_ascii=False)
    tokens = _fabricate_tokens(json_text, uncertain_substrings=[], certain_logprob=-0.3)
    await compute_llm_confidence("דירה", tokens, ["מחיר"], {"מחיר": 100}, Vertical.REAL_ESTATE)

    events = [e["event"] for e in logged_events]
    assert "confidence_embedding_cross_check_unavailable" in events
    final = next(e for e in logged_events if e["event"] == "confidence_computed")
    assert final["embedding_outcome"] == "unavailable_fell_back_to_logprob_only"
