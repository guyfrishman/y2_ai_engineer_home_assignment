from services.normalizer_service import correct_word, normalize_query


def test_expands_number_with_thousand_magnitude_word():
    assert "70000" in normalize_query("עד 70 אלף שח")


def test_expands_number_with_million_magnitude_word():
    assert "2000000" in normalize_query("עד 2 מיליון")


def test_expands_bare_million_word_preserving_surrounding_spacing():
    assert normalize_query("דירה עד מיליון") == "דירה עד 1000000"


def test_rewrites_between_phrase_as_dash_range():
    assert "2018-2021" in normalize_query("רכב בין 2018 ל2021")


def test_canonicalizes_currency_words():
    for variant in ["שח", "ש\"ח", "שקל", "שקלים", "₪"]:
        assert "ש״ח" in normalize_query(f"עד 100 {variant}")


def test_currency_symbol_glued_to_a_digit_still_canonicalizes():
    # ₪ is routinely written with no space ("100₪") -- must not require a
    # word boundary the way the Hebrew-letter currency words do.
    assert "ש״ח" in normalize_query("100₪")


def test_currency_word_does_not_mangle_unrelated_words_it_is_a_prefix_of():
    # Regression: the mark-tolerant "ש״ח" pattern is tolerant of *zero*
    # marks, so without a word boundary it degenerates to a bare "שח"
    # substring match -- "שחור" (black) and "משחק" (game) both start with
    # (or contain) that substring and used to get mangled into "ש״חור"/
    # "מש״חק". This predates mark-tolerance (the original pattern's own
    # literal "שח" alternative already matched "שחור") but is fixed here,
    # same root cause.
    assert normalize_query("מכונית שחורה") != "מכונית ש״חורה"
    assert "ש״ח" not in normalize_query("משחק מחשב")


def test_expands_thousand_shekel_abbreviation_ascii_double_quote():
    assert "20000" in normalize_query('20 אש"ח')


def test_expands_thousand_shekel_abbreviation_doubled_ascii_apostrophe():
    assert "20000" in normalize_query("20 אש''ח")


def test_expands_thousand_shekel_abbreviation_canonical_gershayim():
    assert "20000" in normalize_query("20 אש״ח")


def test_expands_k_suffix():
    assert "10000" in normalize_query("עד 10k")
    assert "10000" in normalize_query("עד 10K")


def test_k_suffix_does_not_swallow_a_different_unit():
    # "50kg" -- the "k" here belongs to a different unit, not a magnitude
    # suffix on its own; must not become "50000g".
    assert "50000" not in normalize_query("משקל 50kg")


def test_jeep_repro_query_still_scores_zero_rule_confidence_after_normalization():
    # The exact bug-repro case: normalization alone must not accidentally
    # manufacture a taxonomy-term/cue-word match out of "ג'יפ" -- that's
    # deliberately left to the zero-signal LLM-classify fallback (item 1),
    # not to normalization or cue-word derivation guessing at it.
    from services.classifier_service import classify_query
    from services.sanitizer_service import sanitize_query

    canonical = normalize_query(sanitize_query("ג'יפ קטן עד 20 אש''ח"))
    assert canonical == "ג'יפ קטן עד 20000 ש״ח"
    assert classify_query(canonical).confidence == 0.0


def test_corrects_known_static_typo():
    assert normalize_query("דירה בירושליים") == "דירה ירושלים"


def test_corrects_typo_with_attached_preposition_prefix():
    # "בתלאביב" = "ב" (in) + "תלאביב" (typo for "תל אביב-יפו"), all one token.
    assert "תל אביב-יפו" in normalize_query("דירה בתלאביב")


def test_does_not_mangle_short_real_word_that_starts_with_a_prefix_letter():
    # "בית" (house) must not be treated as "ב" + "ית".
    assert normalize_query("בית פרטי") == "בית פרטי"


def test_correct_word_is_memoized():
    correct_word.cache_clear()
    correct_word("ירושליים")
    info_after_first_call = correct_word.cache_info()
    correct_word("ירושליים")
    info_after_second_call = correct_word.cache_info()
    assert info_after_second_call.hits == info_after_first_call.hits + 1


def test_stopword_and_short_words_pass_through_unchanged():
    assert normalize_query("דירה עם גינה") == "דירה עם גינה"


def test_lamechira_fuzzy_corrects_toward_real_estate():
    # Disclosed, not fixed: "למכירה" ("for sale") isn't taxonomy vocabulary
    # at all, but it's a close enough fuzzy match (rapidfuzz ratio, one
    # extra leading letter) to real estate's own "מכירה" מצבי_עסקה term to
    # clear FUZZY_MATCH_MIN_SCORE (85) and get corrected to it -- a
    # pre-existing normalizer behavior (fuzzy-matching the whole word before
    # the prefix-stripping fallback ever runs), not something this pass
    # introduced. Its effect: a used-goods "X למכירה" query picks up an
    # unearned real-estate-leaning signal. See
    # test_vehicle_type_words_shared_with_other_verticals_are_a_real_tie in
    # test_classifier_service.py for the analogous, taxonomy-inherent case.
    assert "מכירה" in normalize_query("אלקטרוניקה למכירה").split()
