"""Query normalization: unit/magnitude-word expansion, range-phrase
rewriting, and per-word typo correction. Pure functions, no network calls —
the output is the canonical query string classification, extraction, and
the cache key all key off.
"""

import functools
import re

from rapidfuzz import fuzz, process

from repositories.taxonomy_repository import taxonomy_repository

WORD_CORRECTION_CACHE_SIZE = 8192
FUZZY_MATCH_MIN_SCORE = 85  # rapidfuzz 0-100 similarity; below this, leave the word as-is
MIN_WORD_LENGTH_FOR_CORRECTION = 3  # shorter words (prepositions, "עד", "בן") fuzzy-match unreliably

# Common Hebrew function words, excluded from typo correction (too short/
# ambiguous to fuzzy-match safely) and from the classifier's token-coverage
# scoring (see classifier_service.py) — they carry no vertical signal.
HEBREW_STOPWORDS = frozenset(
    {
        "עם", "בלי", "של", "על", "אל", "עד", "מן", "מ", "ב", "כ", "ל", "ה",
        "ו", "או", "גם", "רק", "כל", "זה", "זו", "אלה", "יש", "אין", "לא",
        "כן", "אני", "אתה", "את", "הוא", "היא", "אנחנו", "הם", "הן", "אבל",
        "אז", "כי", "פה", "שם", "בין", "לפי", "עבור", "אחרי", "לפני",
    }
)

_THOUSAND_MULTIPLIER = 1_000
_MILLION_MULTIPLIER = 1_000_000
_MAGNITUDE_WORD_MULTIPLIERS: dict[str, int] = {
    "אלף": _THOUSAND_MULTIPLIER,
    "אלפים": _THOUSAND_MULTIPLIER,
    "מיליון": _MILLION_MULTIPLIER,
    "מליון": _MILLION_MULTIPLIER,
}
# Two separate patterns, not one with an optional number group: an optional
# leading `\d+` next to a mandatory `\s*` lets the engine match starting at
# the whitespace itself when no number is present, which silently eats the
# space before a bare magnitude word (e.g. "עד מליון" -> "עד1000000").
_NUMBER_THEN_MAGNITUDE_PATTERN = re.compile(
    r"(?P<number>\d+(?:\.\d+)?)\s*(?P<magnitude>" + "|".join(_MAGNITUDE_WORD_MULTIPLIERS) + r")"
)
_BARE_MAGNITUDE_PATTERN = re.compile(
    r"(?<![\d.])(?P<magnitude>" + "|".join(_MAGNITUDE_WORD_MULTIPLIERS) + r")"
)

_BETWEEN_PHRASE_PATTERN = re.compile(r"בין\s+(?P<low>\d+)\s*(?:ל-?|עד)\s*(?P<high>\d+)")

_CURRENCY_WORD_PATTERN = re.compile(r"(ש\"ח|ש״ח|שקלים|שקל|שח|₪)")
_CURRENCY_CANONICAL_FORM = "ש״ח"

_NUMERIC_TOKEN_PATTERN = re.compile(r"^[\d\-.,]+$")
_WORD_EDGE_PUNCTUATION = ",.!?;:"


def _expand_magnitude_words(text: str) -> str:
    """Rewrite "3 מיליון" -> "3000000" and bare "מיליון" -> "1000000"."""

    def replace_number_and_magnitude(match: re.Match[str]) -> str:
        multiplier = _MAGNITUDE_WORD_MULTIPLIERS[match.group("magnitude")]
        return str(int(float(match.group("number")) * multiplier))

    def replace_bare_magnitude(match: re.Match[str]) -> str:
        return str(_MAGNITUDE_WORD_MULTIPLIERS[match.group("magnitude")])

    text = _NUMBER_THEN_MAGNITUDE_PATTERN.sub(replace_number_and_magnitude, text)
    text = _BARE_MAGNITUDE_PATTERN.sub(replace_bare_magnitude, text)
    return text


def _rewrite_between_phrases_as_dash_ranges(text: str) -> str:
    """Rewrite "בין 2018 ל2021" -> "2018-2021", matching how a plain
    "2018-2021" is already written, so extractors only need one pattern."""
    return _BETWEEN_PHRASE_PATTERN.sub(lambda m: f'{m.group("low")}-{m.group("high")}', text)


def _canonicalize_currency_words(text: str) -> str:
    return _CURRENCY_WORD_PATTERN.sub(_CURRENCY_CANONICAL_FORM, text)


# Single-letter Hebrew prepositions/conjunctions commonly attached directly
# to the next word with no separator (ב+ירושלים = "בירושלים" = "in
# Jerusalem"). The static typo map and taxonomy terms are unprefixed, so a
# prefixed typo ("בתלאביב") can fail both the exact map and the fuzzy match
# against the full un-prefixed term. Tried only as a fallback, after the
# word itself fails to correct — stripping blindly would mangle real words
# that happen to start with these letters (e.g. "בית").
_COMMON_HEBREW_PREFIXES = ("ב", "ל", "מ", "ו", "ש", "כ", "ה")


def _correct_single_word(word: str) -> str:
    if word in _TYPO_CORRECTIONS:
        return _TYPO_CORRECTIONS[word]
    if word in taxonomy_repository.term_index:
        return word
    fuzzy_match = process.extractOne(
        word,
        taxonomy_repository.known_terms_by_length,
        scorer=fuzz.ratio,
        score_cutoff=FUZZY_MATCH_MIN_SCORE,
    )
    return fuzzy_match[0] if fuzzy_match else word


@functools.lru_cache(maxsize=WORD_CORRECTION_CACHE_SIZE)
def correct_word(word: str) -> str:
    """Correct a single word: the taxonomy's static typo map first, then a
    fuzzy match against known taxonomy terms, then — only if both of those
    found nothing — the same two checks again with one common leading
    preposition letter stripped.

    Cached per word — normalization runs on every request, and the same
    words (city names, brands, common typos) recur constantly across real
    query traffic, so this cache absorbs most of the fuzzy-matching cost
    after the first time each word is seen.
    """
    direct_correction = _correct_single_word(word)
    if direct_correction != word:
        return direct_correction

    if len(word) > MIN_WORD_LENGTH_FOR_CORRECTION and word[0] in _COMMON_HEBREW_PREFIXES:
        remainder = word[1:]
        if len(remainder) >= MIN_WORD_LENGTH_FOR_CORRECTION:
            remainder_correction = _correct_single_word(remainder)
            if remainder_correction != remainder:
                return remainder_correction

    return word


def _all_typo_corrections() -> dict[str, str]:
    corrections: dict[str, str] = {}
    for typo_map in taxonomy_repository.typo_maps.values():
        corrections.update(typo_map)
    return corrections


_TYPO_CORRECTIONS = _all_typo_corrections()


def _should_correct(word: str) -> bool:
    # An exact static-map hit (e.g. "קמ" -> "ק״מ") is always safe to apply,
    # even for a word shorter than the fuzzy-match length guard below.
    if word in _TYPO_CORRECTIONS:
        return True
    if len(word) < MIN_WORD_LENGTH_FOR_CORRECTION:
        return False
    if word in HEBREW_STOPWORDS:
        return False
    if _NUMERIC_TOKEN_PATTERN.match(word):
        return False
    return True


def normalize_query(sanitized_query: str) -> str:
    """Produce the canonical query string: magnitude words and currency
    words expanded/canonicalized, between-phrases rewritten as dashed
    ranges, and each eligible word typo-corrected.
    """
    text = _expand_magnitude_words(sanitized_query)
    text = _rewrite_between_phrases_as_dash_ranges(text)
    text = _canonicalize_currency_words(text)

    corrected_words = []
    for raw_word in text.split():
        word = raw_word.strip(_WORD_EDGE_PUNCTUATION)
        if not word:
            continue
        corrected_words.append(correct_word(word) if _should_correct(word) else word)

    return " ".join(corrected_words)
