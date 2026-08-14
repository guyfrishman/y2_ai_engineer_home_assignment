"""Measured confidence for a successful LLM-fallback extraction — not a
flat hardcoded band. A fixed constant would report the same number
regardless of whether the completion was genuinely confident or shaky,
which defeats the point of a per-response confidence field.

Two independent signals, blended:
  1. logprob confidence — how probable the model found the specific VALUE
     tokens it emitted (not the surrounding JSON syntax, which sits
     near-100%-probable regardless of correctness and would dilute the
     signal).
  2. embedding cross-check — cosine similarity between the canonical query
     and a synthetic sentence reconstructed from the extracted params, as
     an independent semantic sanity check the logprob signal alone can't
     provide (a model can be high-confidence about a wrong extraction).
"""

import asyncio
import json
import math
from typing import Any

from logger import log_event
from repositories.openai_repository import OpenAIRepository, OpenAIUnavailableError
from schema.taxonomy_models import Vertical

# Initial, tunable weighting — not a fixed law. See docs/DESIGN.md.
LOGPROB_WEIGHT = 0.7
EMBEDDING_WEIGHT = 0.3


def _reconstruct_text_and_token_spans(token_logprobs: list) -> tuple[str, list[tuple[int, int, float]]]:
    """Concatenate completion tokens back into the full text, recording each
    token's character span so a value's text span can be mapped back to the
    tokens that produced it."""
    text_parts: list[str] = []
    spans: list[tuple[int, int, float]] = []
    offset = 0
    for token_logprob in token_logprobs:
        token_text = token_logprob.token
        start, end = offset, offset + len(token_text)
        spans.append((start, end, token_logprob.logprob))
        text_parts.append(token_text)
        offset = end
    return "".join(text_parts), spans


def _find_top_level_value_spans(json_text: str, field_names: list[str]) -> list[tuple[int, int]]:
    """Find the character span of each field's VALUE (not its key) in the
    raw JSON completion text. Uses ``json.JSONDecoder.raw_decode`` to
    consume exactly one value starting right after "field_name": — it
    understands full JSON grammar, so nested objects/arrays (a RangeValue,
    an enum list) are handled correctly with no manual brace-matching.
    """
    decoder = json.JSONDecoder()
    spans: list[tuple[int, int]] = []
    for field_name in field_names:
        key_literal = json.dumps(field_name, ensure_ascii=False)
        key_index = json_text.find(key_literal)
        if key_index == -1:
            continue
        colon_index = json_text.find(":", key_index + len(key_literal))
        if colon_index == -1:
            continue
        value_start = colon_index + 1
        while value_start < len(json_text) and json_text[value_start] in " \t\n\r":
            value_start += 1
        try:
            _, value_end = decoder.raw_decode(json_text, value_start)
        except json.JSONDecodeError:
            continue
        spans.append((value_start, value_end))
    return spans


def compute_logprob_confidence(token_logprobs: list, present_field_names: list[str]) -> float:
    """exp(mean(logprob)) over the tokens that make up the extracted
    fields' VALUES — the geometric mean of each token's probability,
    written as exp-of-mean-logprob since that's the same quantity computed
    more simply. Falls back to averaging every token if the value spans
    can't be located (e.g. an empty params object), so the score still
    reflects something real about this specific completion.
    """
    if not token_logprobs:
        return 0.0
    full_text, token_spans = _reconstruct_text_and_token_spans(token_logprobs)
    value_spans = _find_top_level_value_spans(full_text, present_field_names)

    if value_spans:
        relevant_logprobs = [
            logprob
            for start, end, logprob in token_spans
            if any(start < value_end and end > value_start for value_start, value_end in value_spans)
        ]
    else:
        relevant_logprobs = [logprob for _, _, logprob in token_spans]

    if not relevant_logprobs:
        return 0.0
    mean_logprob = sum(relevant_logprobs) / len(relevant_logprobs)
    return math.exp(mean_logprob)


def _params_to_synthetic_sentence(params: dict[str, Any], vertical: Vertical) -> str:
    """Reconstruct the extracted params as a Hebrew sentence for the
    embedding cross-check, prefixed with the assigned category. An
    out-of-domain extraction (a car query force-extracted under נדל״ן) can
    still score deceptively well on shared incidental field-value keywords
    alone — embedding against "קטגוריה: X, ..." makes the category itself
    part of what gets compared, not just the field values.
    """
    parts = []
    for field_name, value in params.items():
        if isinstance(value, dict):
            bounds = ", ".join(f"{bound_name}={bound_value}" for bound_name, bound_value in value.items())
            parts.append(f"{field_name}: {bounds}")
        elif isinstance(value, list):
            parts.append(f"{field_name}: {', '.join(str(item) for item in value)}")
        else:
            parts.append(f"{field_name}: {value}")
    if not parts:
        return ""
    return f"קטגוריה: {vertical.value}, " + " ".join(parts)


def _cosine_similarity(vector_a: list[float], vector_b: list[float]) -> float:
    dot_product = sum(a * b for a, b in zip(vector_a, vector_b))
    norm_a = math.sqrt(sum(a * a for a in vector_a))
    norm_b = math.sqrt(sum(b * b for b in vector_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot_product / (norm_a * norm_b)


async def compute_embedding_similarity(canonical_query: str, params: dict[str, Any], vertical: Vertical) -> float:
    synthetic_sentence = _params_to_synthetic_sentence(params, vertical)
    if not synthetic_sentence:
        return 0.0
    query_embedding, params_embedding = await asyncio.gather(
        OpenAIRepository.embed(canonical_query), OpenAIRepository.embed(synthetic_sentence)
    )
    return _cosine_similarity(query_embedding, params_embedding)


async def compute_llm_confidence(
    canonical_query: str,
    token_logprobs: list,
    present_field_names: list[str],
    validated_params: dict[str, Any],
    vertical: Vertical,
) -> float:
    """Blends two independent signals, always -- a model can be
    high-confidence about tokens it typed while being categorically wrong
    about what it should have been asked at all, so a decisive logprob
    score alone is no longer treated as license to skip the embedding
    cross-check (see docs/DESIGN.md for the measured latency/cost this
    costs, and why it's paid on every LLM-fallback response now, not just
    the ones the old logprob-decisiveness heuristic judged borderline).
    """
    logprob_confidence = compute_logprob_confidence(token_logprobs, present_field_names)

    try:
        embedding_similarity = await compute_embedding_similarity(canonical_query, validated_params, vertical)
        embedding_outcome = "success"
    except OpenAIUnavailableError as error:
        # A successful extraction shouldn't be thrown away because the
        # unrelated embedding cross-check couldn't run — fall back to the
        # logprob signal alone rather than failing the whole tier. Logged
        # explicitly: silently substituting logprob_confidence for the
        # embedding signal means this response's "confidence" no longer
        # reflects an independent cross-check at all, on a path (LLM
        # fallback -> confidence scoring) where that distinction matters.
        embedding_similarity = logprob_confidence
        embedding_outcome = "unavailable_fell_back_to_logprob_only"
        log_event(
            event="confidence_embedding_cross_check_unavailable",
            vertical=vertical.value,
            reason=str(error),
            logprob_confidence=round(logprob_confidence, 4),
        )

    confidence = LOGPROB_WEIGHT * logprob_confidence + EMBEDDING_WEIGHT * embedding_similarity
    confidence = min(max(confidence, 0.0), 1.0)
    # The headline number a caller sees is the blend; without this, there is
    # no way from the logs to tell whether a low (or a deceptively high)
    # confidence came from the model's own token-level uncertainty, a
    # semantic mismatch the embedding cross-check caught, or both.
    log_event(
        event="confidence_computed",
        vertical=vertical.value,
        logprob_confidence=round(logprob_confidence, 4),
        embedding_similarity=round(embedding_similarity, 4),
        embedding_outcome=embedding_outcome,
        confidence=round(confidence, 4),
    )
    return confidence
