import pytest

from app.config import settings
from app.services.sanitizer_service import QueryRejectedError, sanitize_query


def test_plain_hebrew_query_passes_through_unchanged():
    assert sanitize_query("דירת 3 חדרים בירושלים") == "דירת 3 חדרים בירושלים"


def test_strips_emoji():
    assert sanitize_query("דירה 🏠 בתל אביב 😀") == "דירה  בתל אביב"


def test_strips_control_characters():
    assert sanitize_query("דירה\x00\x01 בתל אביב") == "דירה בתל אביב"


def test_strips_zero_width_and_bidi_control_characters():
    # U+200E (LRM), U+202E (RLO) — used in unicode-trick injection attempts.
    assert sanitize_query("דירה‎‮ בתל אביב") == "דירה בתל אביב"


def test_truncates_to_max_query_length():
    huge_query = "דירה " * (settings.max_query_length // 5 + 200)
    result = sanitize_query(huge_query)
    assert len(result) <= settings.max_query_length


def test_empty_query_is_rejected():
    with pytest.raises(QueryRejectedError):
        sanitize_query("")


def test_query_that_is_only_emoji_is_rejected():
    with pytest.raises(QueryRejectedError):
        sanitize_query("🏠🚗😀")


def test_whitespace_only_query_is_rejected():
    with pytest.raises(QueryRejectedError):
        sanitize_query("    \n\t  ")
