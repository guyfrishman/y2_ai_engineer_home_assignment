from app.services.normalizer_service import correct_word, normalize_query


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
