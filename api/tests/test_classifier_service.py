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
    result = _classify("מאזדה CX-5 שנת 2020")
    assert result.confidence >= 0.5


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
