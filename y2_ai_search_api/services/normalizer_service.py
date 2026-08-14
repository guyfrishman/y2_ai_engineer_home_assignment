"""Query normalization: unit/magnitude-word expansion, range-phrase
rewriting, and per-word typo correction. Pure functions, no network calls —
the output is the canonical query string classification, extraction, and
the cache key all key off.
"""

import functools
import re

from rapidfuzz import fuzz, process

from repositories.taxonomy_repository import HEBREW_STOPWORDS, taxonomy_repository
from text_normalization import build_mark_tolerant_alternation, build_mark_tolerant_pattern

WORD_CORRECTION_CACHE_SIZE = 8192
FUZZY_MATCH_MIN_SCORE = 85  # rapidfuzz 0-100 similarity; below this, leave the word as-is
MIN_WORD_LENGTH_FOR_CORRECTION = 3  # shorter words (prepositions, "עד", "בן") fuzzy-match unreliably

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

_CURRENCY_WORD_PATTERN = re.compile(
    # Word-bounded on both sides -- (?<!\w)/(?!\w), not (?<!\S)/(?!\S), so a
    # currency word directly followed by punctuation ("שח.", "שח,") still
    # matches. Without *some* trailing boundary, the mark-tolerant "ש״ח"
    # pattern (tolerant of *zero* marks) degenerates to a bare "שח"
    # substring match anywhere in the text, including inside unrelated
    # words like "שחור" (black) or "משחק" (game). Confirmed reproducible
    # pre-existing behavior even before mark-tolerance (the original
    # pattern's own literal "שח" alternative already matched "שחור"'s
    # first two letters) -- fixed here since it's the same root cause.
    #
    # ₪ is deliberately its OWN, unbounded alternative, not inside the
    # word-boundary group: it's a currency *symbol*, not a letter sequence,
    # so it has none of the above substring-collision risk, and it's
    # routinely written glued directly to a digit ("100₪") -- a real,
    # common case a word boundary would otherwise reject.
    r"(?<!\w)(?:" + build_mark_tolerant_alternation("ש״ח", "שקלים", "שקל", "שח") + r")(?!\w)|₪"
)
_CURRENCY_CANONICAL_FORM = "ש״ח"

# "אש״ח" (אלף ש״ח, thousand NIS) — numeric-language grammar, same category
# as _MAGNITUDE_WORD_MULTIPLIERS above, not taxonomy vocabulary, so it's a
# small hardcoded set like that one, not something derived from the
# taxonomy. Mark-tolerant so "אש\"ח"/"אש''ח"/"אש״ח" all expand the same way.
# Multiplies *and* emits the canonical currency word, feeding the exact same
# _extract_price/_CURRENCY_UNIT_PATTERN path in extractor_service.py as a
# plain "20000 ש״ח" would — no extraction-side change needed. Must run
# before _canonicalize_currency_words, which would otherwise partially
# match the "ש״ח" tail inside "אש״ח" and leave a stray "א" behind.
_THOUSAND_SHEKEL_PATTERN = re.compile(
    r"(?P<number>\d+(?:\.\d+)?)\s*" + build_mark_tolerant_pattern("אש״ח") + r"(?!\w)"
)
# "10k"/"10K" -> "10000": a bare numeric-magnitude suffix, same mechanism as
# _MAGNITUDE_WORD_MULTIPLIERS. The negative lookahead excludes a unit like
# "50kg" ("k" here is part of a different unit, not a magnitude suffix).
_K_SUFFIX_PATTERN = re.compile(r"(?P<number>\d+(?:\.\d+)?)[kK](?![a-zA-Zא-ת])")

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


def _expand_thousand_shekel_abbreviation(text: str) -> str:
    """Rewrite "20 אש״ח" -> "20000 ש״ח" (any mark variant of אש״ח) — feeds
    the same _extract_price/_CURRENCY_UNIT_PATTERN path as a plain "20000
    ש״ח" would. Must run before _canonicalize_currency_words, which would
    otherwise partially match the "ש״ח" tail inside "אש״ח" and leave a
    stray "א" behind.
    """

    def replace(match: re.Match[str]) -> str:
        return f"{int(float(match.group('number')) * _THOUSAND_MULTIPLIER)} {_CURRENCY_CANONICAL_FORM}"

    return _THOUSAND_SHEKEL_PATTERN.sub(replace, text)


def _expand_k_suffix(text: str) -> str:
    """Rewrite "10k"/"10K" -> "10000"."""

    def replace(match: re.Match[str]) -> str:
        return str(int(float(match.group("number")) * _THOUSAND_MULTIPLIER))

    return _K_SUFFIX_PATTERN.sub(replace, text)


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
    """Produce the canonical query string: financial-slang and magnitude
    words expanded, currency canonicalized, between-phrases rewritten as
    dashed ranges, and each eligible word typo-corrected.
    """
    text = _expand_thousand_shekel_abbreviation(sanitized_query)
    text = _expand_k_suffix(text)
    text = _expand_magnitude_words(text)
    text = _rewrite_between_phrases_as_dash_ranges(text)
    text = _canonicalize_currency_words(text)

    corrected_words = []
    for raw_word in text.split():
        word = raw_word.strip(_WORD_EDGE_PUNCTUATION)
        if not word:
            continue
        corrected_words.append(correct_word(word) if _should_correct(word) else word)

    return " ".join(corrected_words)
