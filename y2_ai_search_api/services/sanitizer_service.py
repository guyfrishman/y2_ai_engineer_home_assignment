import unicodedata
from config import settings
from logger import log_event

_DISALLOWED_UNICODE_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "So", "Sk"})


class QueryRejectedError(ValueError):
    """Raised when a query is empty, or becomes empty, after sanitization."""


def _is_disallowed_character(character: str) -> bool:
    return unicodedata.category(character) in _DISALLOWED_UNICODE_CATEGORIES


def sanitize_query(raw_query: str) -> str:
    """Strip control characters and emoji, then cap the result to
    ``settings.max_query_length``. Raises ``QueryRejectedError`` if nothing
    usable is left."""
    disallowed_character_count = sum(
        1 for character in raw_query if _is_disallowed_character(character)
    )
    cleaned = "".join(
        character for character in raw_query if not _is_disallowed_character(character)
    ).strip()

    if disallowed_character_count:
        log_event(
            event="security_input_rejected",
            reason="disallowed_characters_stripped",
            stripped_count=disallowed_character_count,
        )

    if len(cleaned) > settings.max_query_length:
        log_event(
            event="security_input_rejected",
            reason="max_length_exceeded",
            original_length=len(raw_query),
            max_length=settings.max_query_length,
        )
        cleaned = cleaned[: settings.max_query_length].strip()

    if not cleaned:
        log_event(event="security_input_rejected", reason="empty_after_sanitization")
        raise QueryRejectedError("query is empty after sanitization")

    return cleaned
