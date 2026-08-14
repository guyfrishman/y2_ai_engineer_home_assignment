"""Mark-tolerant pattern generation — shared by repositories/taxonomy_repository.py,
services/classifier_service.py, services/extractor_service.py, and
services/normalizer_service.py, none of which may import each other in a
cycle. Top-level utility module for that reason, alongside config.py/
logger.py/metrics.py.
"""

import re

_WORD_CHAR_PATTERN = re.compile(r"\w", re.UNICODE)


def _char_kind(char: str) -> str:
    if char.isspace():
        return "space"
    if _WORD_CHAR_PATTERN.match(char):
        return "word"
    return "mark"


def build_mark_tolerant_pattern(literal_text: str) -> str:
    """Turn a literal string into a regex pattern where every run of "mark"
    characters (geresh, gershayim, ASCII/curly quotes, slashes, parentheses,
    hyphens, ... — anything that's neither alphanumeric nor whitespace)
    becomes "zero or more marks of any kind", and word/whitespace runs stay
    literal. Word-boundary requirements (spaces between actual words) are
    untouched — only punctuation *within* a token is tolerant.

    Not a hand-kept list of which quote characters count: str/re's own
    Unicode word-character classification already knows geresh (׳), gershayim
    (״), an ASCII apostrophe, and a curly quote are all "not a word
    character" — so קוטג׳, קוטג', and קוטג all compile to the same pattern,
    matching all three, because the mark position IS "any or no punctuation
    here" by construction, not a specific enumerated character list.

    Used both for taxonomy-term matching (repositories/taxonomy_repository.py,
    services/classifier_service.py — matched_text there is always the
    canonical taxonomy term, not the regex match, so whatever mark variant a
    user typed, extracted field values stay taxonomy-exact) and for the
    small set of hardcoded unit/currency abbreviations this codebase already
    hand-maintains (ש״ח, כ״ס, מ״ר, ...), which used to hand-enumerate two
    mark variants per pattern and silently miss a third.
    """
    if not literal_text:
        return ""

    runs: list[tuple[str, str]] = []
    for char in literal_text:
        kind = _char_kind(char)
        if runs and runs[-1][0] == kind:
            runs[-1] = (kind, runs[-1][1] + char)
        else:
            runs.append((kind, char))

    pattern_parts: list[str] = []
    for kind, run in runs:
        if kind == "word":
            pattern_parts.append(re.escape(run))
        elif kind == "space":
            pattern_parts.append(r"\s+")
        else:
            pattern_parts.append(r"[^\w\s]*")
    return "".join(pattern_parts)


def build_mark_tolerant_alternation(*canonical_words: str) -> str:
    """Non-capturing alternation of mark-tolerant patterns, one per
    canonical word — e.g. build_mark_tolerant_pattern("ש״ח") already
    matches "ש\\"ח"/"ש''ח"/"ש״ח" on its own, so a unit/currency pattern only
    needs to list each *distinct* word once, not every mark variant of it
    by hand.
    """
    return "(?:" + "|".join(build_mark_tolerant_pattern(word) for word in canonical_words) + ")"
