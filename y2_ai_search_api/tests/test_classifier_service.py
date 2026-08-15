from config import settings
from schema.taxonomy_models import Vertical
from services.classifier_service import classify_query
from services.normalizer_service import normalize_query
from services.sanitizer_service import sanitize_query


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
    # name and comment actually make. Deliberately "2020", not "שנת 2020":
    # "שנת" (construct-state "year of") is a real, taxonomy-derived
    # יד_שנייה cue word too (from the "שנת_ייצור" field name split) -- the
    # bare year alone still extracts fine (extractor_service's year regex
    # doesn't need the word "שנת" at all), without introducing that
    # cross-vertical competition into what this test means to keep clean.
    result = _classify("מאזדה CX-5 2020")
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


def test_bait_alone_signals_real_estate_without_villa():
    # "בית פרטי/וילה" is one literal taxonomy string -- bare "בית" ("house")
    # used to match nothing at all. Now derived as a real-estate cue word
    # (TaxonomyRepository._build_cue_words, Rule C), not hand-added.
    result = _classify("בית גדול ונעים")
    assert result.vertical == Vertical.REAL_ESTATE
    assert result.confidence > 0.0


def test_for_sale_phrasing_no_longer_tips_ambiguously_toward_used_goods():
    # "למכירה"/"מכירה" are transaction words ambiguous across all three
    # verticals (a car or an apartment is also "for sale") -- and "מכירה"
    # is literally a real-estate מצבי_עסקה term. A used-goods-only cue word
    # for it used to fight the real-estate signal on exactly this kind of
    # query.
    result = _classify("דירה למכירה בחיפה")
    assert result.vertical == Vertical.REAL_ESTATE


def test_vehicle_type_words_shared_with_other_verticals_are_a_real_tie():
    # "מסחרי" ("commercial") is literally both a vehicles סוגי_רכב value
    # *and* a real-estate מצבי_עסקה value -- the taxonomy itself makes this
    # word genuinely ambiguous, not a gap in cue-word derivation. Scored as
    # a real 1-1 tie (see margin_factor); classify_query's own tie-break
    # still favors Vertical.REAL_ESTATE (declared first, same mechanics as
    # the zero-confidence default) at the classify_query level -- but
    # is_tied=True now means the pipeline (services.parse_service) doesn't
    # trust that pick as a hint: it routes through the classify-only LLM
    # call instead, same as confidence == 0.0. See
    # test_zero_signal_classification.py for the full-pipeline behavior.
    result = _classify("מסחרי עד 100000 שח")
    assert result.vertical == Vertical.REAL_ESTATE  # the tie-break, documented
    assert 0.0 < result.confidence < 0.5  # genuinely low, not confidently wrong
    assert result.is_tied is True


def test_furniture_material_word_no_longer_competes_with_a_real_furniture_match():
    # "שולחן" (table) correctly matches יד_שנייה's סוגי_רהיט. "מלא" (in
    # "אלון מלא", solid oak) is also a real, literal נדל״ן ריהוט value
    # (furnished status: ללא/חלקי/מלא) -- but ריהוט is a general_attributes
    # field (a property of an already-identified item, not identifying
    # signal), so it no longer counts toward classification score at all.
    # יד_שנייה wins outright now, no tie, no coin flip.
    result = _classify("שולחן אבירים אלון מלא פרדס חנה כרכור 3000 שח")
    assert result.is_tied is False
    assert result.vertical == Vertical.USED_GOODS
    assert result.confidence > 0.0


def test_typo_corrected_word_no_longer_ties_reaches_zero_signal_instead():
    # "טאבון" (tabun oven) no longer fuzzy-corrupts to "טאבו" (land-registry
    # status) -- FUZZY_MATCH_MIN_SCORE was raised past both words' 88.89
    # similarity score. "גז" (gas) still matches רכב's own סוג_דלק, but
    # סוג_דלק is general_attributes too, so it no longer counts toward
    # score either. Zero identifying signal for any vertical -- the clean
    # zero-signal gate (confidence == 0.0) catches this directly now, no
    # tie-detection needed.
    result = _classify("טאבון גז אוני קודה 16 מודיעין 2000 שח")
    assert result.confidence == 0.0
    assert result.is_tied is False


def test_generic_fuel_type_word_alone_reaches_zero_signal():
    # "חשמלי" (electric) is a real רכב סוג_דלק value, but describes ovens,
    # bikes, guitars just as often as cars -- general_attributes exclusion
    # means it no longer manufactures a confident (wrong) vehicles pick on
    # its own for a non-vehicle query.
    result = _classify("תנור אפייה חשמלי כרמיאל 1200 שח")
    assert result.confidence == 0.0


def test_general_attribute_term_matches_are_excluded_from_scoring_not_from_occurrences():
    # "מלא" is still recorded in term_occurrences (extraction needs it once
    # a vertical is otherwise established) -- it's just excluded from the
    # score that decides which vertical wins in the first place.
    canonical = normalize_query(sanitize_query("דירת 3 חדרים מלא בירושלים עד מליון שח"))
    result = classify_query(canonical)
    assert result.vertical == Vertical.REAL_ESTATE
    assert any(o.matched_text == "מלא" for o in result.term_occurrences)
