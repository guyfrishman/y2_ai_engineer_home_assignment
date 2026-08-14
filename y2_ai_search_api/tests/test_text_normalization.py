import re

from text_normalization import build_mark_tolerant_alternation, build_mark_tolerant_pattern


def test_matches_the_canonical_form_itself():
    pattern = re.compile(build_mark_tolerant_pattern("קוטג׳"))
    assert pattern.fullmatch("קוטג׳")


def test_matches_ascii_apostrophe_variant():
    pattern = re.compile(build_mark_tolerant_pattern("קוטג׳"))
    assert pattern.fullmatch("קוטג'")


def test_matches_curly_quote_variant():
    pattern = re.compile(build_mark_tolerant_pattern("קוטג׳"))
    assert pattern.fullmatch("קוטג’")


def test_matches_with_the_mark_dropped_entirely():
    pattern = re.compile(build_mark_tolerant_pattern("קוטג׳"))
    assert pattern.fullmatch("קוטג")


def test_gershayim_variants_all_match_one_pattern():
    pattern = re.compile(build_mark_tolerant_pattern("ש״ח"))
    for variant in ["ש״ח", 'ש"ח', "ש''ח", "שח"]:
        assert pattern.fullmatch(variant), variant


def test_does_not_merge_separate_words_across_a_required_space():
    # Punctuation *within* a token folds away; whitespace between words
    # stays a real word boundary.
    pattern = re.compile(build_mark_tolerant_pattern("תל אביב-יפו"))
    assert pattern.fullmatch("תל אביב-יפו")
    assert pattern.fullmatch("תל אביב יפו") is None  # space instead of "-": not a mark position
    assert pattern.fullmatch("תלאביב-יפו") is None  # missing the space entirely: not tolerated


def test_alternation_matches_any_of_its_canonical_words():
    pattern = re.compile(build_mark_tolerant_alternation("ק״מ", "קילומטר", "קילומטרים"))
    assert pattern.fullmatch("ק\"מ")
    assert pattern.fullmatch("קילומטר")
    assert pattern.fullmatch("קילומטרים")


def test_empty_string_returns_empty_pattern():
    assert build_mark_tolerant_pattern("") == ""
