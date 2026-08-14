"""Cue-word derivation (TaxonomyRepository._build_cue_words) is mechanical,
not hand-picked -- these tests assert the derivation *rules* directly,
against the real vendored taxonomy, rather than trusting a hardcoded word
list. See docs/DESIGN.md for the four rules (self-name, field-name parts,
value parts, collision/redundancy pruning).
"""

from repositories.taxonomy_repository import taxonomy_repository
from schema.taxonomy_models import Vertical


def test_bait_is_derived_as_a_real_estate_cue_word():
    # "בית פרטי/וילה" is one literal multi-word taxonomy string -- "בית"
    # alone is unreachable as an exact term, so it must come from Rule C
    # (splitting that value into words) to be usable at all.
    assert "בית" in taxonomy_repository.cue_words[Vertical.REAL_ESTATE]


def test_prati_is_ambiguous_across_verticals_and_dropped_from_both():
    # "פרטי" is a literal value under both real estate's own general
    # attributes (בעלות) and vehicles' (סוגי_רכב/בעלות) -- Rule D's
    # collision check must drop it from every vertical's cue words, not
    # arbitrarily assign it to one.
    for vertical in Vertical:
        assert "פרטי" not in taxonomy_repository.cue_words[vertical]


def test_dira_is_redundant_with_an_exact_taxonomy_term_and_excluded():
    # "דירה" is already an exact סוגי_נכס value, scored once via
    # _scan_term_occurrences -- keeping it as a cue word too would
    # double-count it.
    assert "דירה" in taxonomy_repository.term_index
    for vertical in Vertical:
        assert "דירה" not in taxonomy_repository.cue_words[vertical]


def test_vehicles_own_name_is_a_cue_word_for_itself():
    assert Vertical.VEHICLES.value in taxonomy_repository.cue_words[Vertical.VEHICLES]


def test_no_cue_word_is_shared_across_two_verticals():
    all_pairs = [(a, b) for a in Vertical for b in Vertical if a != b]
    for vertical_a, vertical_b in all_pairs:
        overlap = taxonomy_repository.cue_words[vertical_a] & taxonomy_repository.cue_words[vertical_b]
        assert not overlap, f"{vertical_a} and {vertical_b} share cue words: {overlap}"


def test_no_cue_word_is_itself_an_exact_reachable_taxonomy_term():
    for vertical in Vertical:
        for word in taxonomy_repository.cue_words[vertical]:
            assert word not in taxonomy_repository.term_index


def test_no_cue_word_is_a_stopword_or_too_short():
    from repositories.taxonomy_repository import HEBREW_STOPWORDS, MIN_CUE_WORD_LENGTH

    for vertical in Vertical:
        for word in taxonomy_repository.cue_words[vertical]:
            assert word not in HEBREW_STOPWORDS
            assert len(word) >= MIN_CUE_WORD_LENGTH
