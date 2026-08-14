
import re
from dataclasses import dataclass

from repositories.taxonomy_repository import HEBREW_STOPWORDS, taxonomy_repository
from schema.taxonomy_models import Vertical
from text_normalization import build_mark_tolerant_pattern

MARGIN_FACTOR_MIN = 0.5
MARGIN_FACTOR_MAX = 1.0

NUMERIC_TOKEN_PATTERN = re.compile(r"^[\d\-.,]+$")

# Words that signal a vertical even though they're never a taxonomy
# *value* — only part of a field's *name* or everyday phrasing (e.g.
# "חדרים" signals נדל״ן's מס׳_חדרים field, but "חדרים" itself never appears
# as a taxonomy enum value). Mechanically derived from the taxonomy itself
# — see TaxonomyRepository._build_cue_words — not a hand-authored list: a
# word not in the taxonomy, and not a sub-word of anything in it, isn't a
# cue word here regardless of how useful it might be in principle; it's
# what services.llm_fallback_service.run_category_classification (the
# zero-signal LLM-classify fallback) exists for instead.
VERTICAL_CUE_WORDS: dict[Vertical, frozenset[str]] = taxonomy_repository.cue_words


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
# Hebrew/Latin/digit mixes (e.g. "iPhone 13"). build_mark_tolerant_pattern,
# not re.escape: a term's internal punctuation (geresh, gershayim, slashes,
# ...) is never the load-bearing part of a match — "קוטג׳"/"קוטג'"/"קוטג"
# all match the same compiled pattern. matched_text stored below is always
# the canonical `term`, never the regex match, so this never lets a
# non-canonical mark variant leak into an extracted field value.
_COMPILED_TERM_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (term, re.compile(rf"(?<!\S){build_mark_tolerant_pattern(term)}(?!\S)"))
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


def classify_query(canonical_query: str) -> ClassificationResult:
    """Pick the vertical with the strongest taxonomy-term coverage and score
    a rule-path confidence for that pick.

    confidence = coverage_ratio * margin_factor, where coverage_ratio is the
    fraction of non-stopword query tokens explained by the winning vertical
    (its own matched terms/cue words, plus any numeric token — numbers are
    treated as signal regardless of vertical, but only once the winning
    vertical already has at least one genuine taxonomy TERM match; see
    docs/DESIGN.md) and margin_factor rewards a clear winner over a
    near-tie with the runner-up vertical.
    """
    words = canonical_query.split()
    occurrences = _scan_term_occurrences(canonical_query)
    scores = _vertical_scores(occurrences, words)
    winning_vertical = max(scores, key=lambda vertical: scores[vertical])

    non_stopword_words = [word for word in words if word not in HEBREW_STOPWORDS]
    winning_matched_word_count = _matched_word_count(occurrences, winning_vertical)
    # Numbers only count as "explained" once there's a real taxonomy term
    # match giving them interpretive context (a matched brand, city,
    # property type, ...). A cue word alone ("רכב") is too weak a signal —
    # it doesn't say what a bare "300" next to it even means (price? km?
    # year?) — so without an actual term match, a query that's mostly
    # numbers stays honestly low-confidence instead of scoring as if fully
    # understood. See docs/DESIGN.md.
    numeric_word_count = (
        sum(1 for word in non_stopword_words if NUMERIC_TOKEN_PATTERN.match(word))
        if winning_matched_word_count > 0
        else 0
    )
    matched_signal_tokens = (
        winning_matched_word_count
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
