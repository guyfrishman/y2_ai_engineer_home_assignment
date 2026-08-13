"""Rule-based vertical detection: scans the canonical query for known
taxonomy terms, picks the vertical with the strongest coverage, and scores
a confidence for that decision. The term occurrences found here are reused
by ``extractor_service`` — no need to re-scan the query a second time."""

import re
from dataclasses import dataclass

from app.logger import log_activity
from app.repositories.taxonomy_repository import taxonomy_repository
from app.schema.taxonomy_models import Vertical
from app.services.normalizer_service import HEBREW_STOPWORDS

# Rewards a clear single-vertical winner over a near-tie between the top two
# verticals. A tie (margin 0) still earns half credit, since ambiguity
# between verticals isn't the same as finding no signal at all.
MARGIN_FACTOR_MIN = 0.5
MARGIN_FACTOR_MAX = 1.0

NUMERIC_TOKEN_PATTERN = re.compile(r"^[\d\-.,]+$")

# Common Hebrew words that signal a vertical even though they're never a
# taxonomy *value* — only part of a field's *name* or everyday phrasing
# (e.g. "חדרים" signals נדל״ן's מס׳_חדרים field, but "חדרים" itself never
# appears as a taxonomy enum value). Kept separate from
# TaxonomyRepository.term_index, which stays strictly taxonomy-value-derived.
VERTICAL_CUE_WORDS: dict[Vertical, frozenset[str]] = {
    Vertical.REAL_ESTATE: frozenset(
        {"חדר", "חדרים", "קומה", "קומות", "דירה", "דירות", "שכירות", "השכרה", "משכנתא", "נדלן", "נדל״ן"}
    ),
    Vertical.VEHICLES: frozenset(
        {"רכב", "מכונית", "אוטו", "ק״מ", "קילומטר", "קילומטרים", "יד", "גיר", "תיבה", "דלק", "טסט"}
    ),
    Vertical.USED_GOODS: frozenset(
        {"משומש", "יד_שנייה", "מכירה", "למכירה"}
    ),
}


@dataclass(frozen=True)
class TermOccurrence:
    """One taxonomy term found in the canonical query — the unit both the
    classifier's coverage score and the extractor's field-filling work off."""

    matched_text: str
    vertical: Vertical
    field_name: str


@dataclass(frozen=True)
class ClassificationResult:
    vertical: Vertical
    confidence: float
    term_occurrences: list[TermOccurrence]


# Precompiled once at import: longest terms first, so a multi-word term like
# "תל אביב-יפו" is matched before any single-word term that happens to be a
# substring of it. `(?<!\S)`/`(?!\S)` require whitespace (or string edge) on
# both sides — a plain word-boundary `\b` behaves inconsistently across
# Hebrew/Latin/digit mixes (e.g. "iPhone 13").
_COMPILED_TERM_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (term, re.compile(rf"(?<!\S){re.escape(term)}(?!\S)"))
    for term in taxonomy_repository.known_terms_by_length
]


def _scan_term_occurrences(canonical_query: str) -> list[TermOccurrence]:
    """Greedily match known taxonomy terms in the query, longest first,
    replacing each match with placeholder characters so a shorter term
    can't re-match text already claimed by a longer one."""
    occurrences: list[TermOccurrence] = []
    working_text = canonical_query
    for term, pattern in _COMPILED_TERM_PATTERNS:
        match = pattern.search(working_text)
        if not match:
            continue
        for term_match in taxonomy_repository.term_index[term]:
            occurrences.append(TermOccurrence(term, term_match.vertical, term_match.field_name))
        working_text = working_text[: match.start()] + ("\0" * len(term)) + working_text[match.end() :]
    return occurrences


def _matched_word_count(occurrences: list[TermOccurrence], vertical: Vertical) -> int:
    return sum(len(occurrence.matched_text.split()) for occurrence in occurrences if occurrence.vertical == vertical)


def _cue_word_count(words: list[str], vertical: Vertical) -> int:
    cue_words = VERTICAL_CUE_WORDS[vertical]
    return sum(1 for word in words if word in cue_words)


def _vertical_scores(occurrences: list[TermOccurrence], words: list[str]) -> dict[Vertical, int]:
    return {
        vertical: _matched_word_count(occurrences, vertical) + _cue_word_count(words, vertical)
        for vertical in Vertical
    }


def _margin_factor(scores: dict[Vertical, int]) -> float:
    ranked = sorted(scores.values(), reverse=True)
    top_score = ranked[0]
    if top_score == 0:
        return MARGIN_FACTOR_MIN
    second_score = ranked[1] if len(ranked) > 1 else 0
    margin = (top_score - second_score) / top_score
    return MARGIN_FACTOR_MIN + (MARGIN_FACTOR_MAX - MARGIN_FACTOR_MIN) * margin


@log_activity
def classify_query(canonical_query: str) -> ClassificationResult:
    """Pick the vertical with the strongest taxonomy-term coverage and score
    a rule-path confidence for that pick.

    confidence = coverage_ratio * margin_factor, where coverage_ratio is the
    fraction of non-stopword query tokens explained by the winning vertical
    (its own matched terms/cue words, plus any numeric token — numbers are
    treated as signal regardless of vertical) and margin_factor rewards a
    clear winner over a near-tie with the runner-up vertical.
    """
    words = canonical_query.split()
    occurrences = _scan_term_occurrences(canonical_query)
    scores = _vertical_scores(occurrences, words)
    winning_vertical = max(scores, key=lambda vertical: scores[vertical])

    non_stopword_words = [word for word in words if word not in HEBREW_STOPWORDS]
    numeric_word_count = sum(1 for word in non_stopword_words if NUMERIC_TOKEN_PATTERN.match(word))
    matched_signal_tokens = (
        _matched_word_count(occurrences, winning_vertical)
        + _cue_word_count(non_stopword_words, winning_vertical)
        + numeric_word_count
    )
    total_signal_tokens = len(non_stopword_words)

    if total_signal_tokens == 0:
        coverage_ratio = 0.0
    else:
        coverage_ratio = min(matched_signal_tokens, total_signal_tokens) / total_signal_tokens

    confidence = coverage_ratio * _margin_factor(scores)
    winning_occurrences = [occurrence for occurrence in occurrences if occurrence.vertical == winning_vertical]

    return ClassificationResult(
        vertical=winning_vertical,
        confidence=round(min(max(confidence, 0.0), 1.0), 4),
        term_occurrences=winning_occurrences,
    )
