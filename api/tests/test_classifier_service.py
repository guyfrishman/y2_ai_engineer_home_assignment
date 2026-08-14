from app.config import settings
from app.schema.taxonomy_models import Vertical
from app.services.classifier_service import classify_query
from app.services.normalizer_service import normalize_query
from app.services.sanitizer_service import sanitize_query


def _classify(raw_query: str):
    canonical = normalize_query(sanitize_query(raw_query))
    return classify_query(canonical)


def test_real_estate_query_classifies_correctly():
    result = _classify("דירת 3 חדרים בירושלים עד מליון שח")
    assert result.vertical == Vertical.REAL_ESTATE


def test_vehicle_query_classifies_correctly():
    result = _classify("טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן")
    assert result.vertical == Vertical.VEHICLES


def test_used_goods_query_classifies_correctly():
    result = _classify("אייפון 13 פרו 256 גיגה כחול כמו חדש עד 2500")
    assert result.vertical == Vertical.USED_GOODS


def test_clean_unambiguous_vehicle_query_has_high_confidence():
    # No cross-vertical term overlap (unlike colors/cities), so this should
    # score well above the confidence threshold and skip the LLM fallback.
    # Asserted against the real threshold, not a literal that could drift
    # out of sync with it and silently stop testing the claim this test's
    # name and comment actually make.
    result = _classify("מאזדה CX-5 שנת 2020")
    assert result.confidence >= settings.confidence_threshold


def test_low_signal_query_has_low_confidence():
    result = _classify("שלום מה קורה איך הולך")
    assert result.confidence < 0.3


def test_empty_signal_defaults_to_zero_confidence():
    # classify_query itself, not the full pipeline — sanitize_query already
    # rejects an empty query before classification would ever see one.
    result = classify_query("")
    assert result.confidence == 0.0


def test_confidence_is_bounded_between_zero_and_one():
    for query in [
        "דירת 3 חדרים בירושלים עד מליון שח",
        "טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן",
        "שלום מה קורה",
    ]:
        result = _classify(query)
        assert 0.0 <= result.confidence <= 1.0


def test_bare_numbers_with_no_taxonomy_term_match_score_zero_confidence():
    # Before the fix, numeric tokens counted as "explained" regardless of
    # whether anything actually explained them -- this query has zero
    # taxonomy term matches and zero cue words, yet used to score 0.5
    # confidence purely from three arbitrary numbers.
    result = _classify("123 456 789")
    assert result.confidence == 0.0


def test_cue_word_plus_unexplained_numbers_does_not_score_near_certain():
    # Before the fix this scored a *maximal* 1.0 confidence: "רכב" (a cue
    # word, not a taxonomy term match) gave the numbers a vertical to
    # attach to, and every one of the five arbitrary numbers then counted
    # as "explained" even though nothing says whether they're a price, a
    # year, or a km reading. A cue word alone isn't enough context for
    # that; only a genuine taxonomy term match (a matched brand, city,
    # property type, ...) is.
    result = _classify("רכב 100 200 300 400 500")
    assert result.confidence < 0.3


def test_numbers_still_count_once_a_real_term_match_gives_them_context():
    # The gate only blocks numeric credit when the winning vertical has
    # *no* real term match at all -- once there's a genuine match (a
    # brand, in this case), numbers go back to counting normally, same as
    # every golden example already relies on.
    result = _classify("טויוטה 2018 2019 2020")
    assert result.confidence > 0.3
