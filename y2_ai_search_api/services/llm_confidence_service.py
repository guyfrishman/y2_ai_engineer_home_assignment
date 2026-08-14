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

from repositories.openai_repository import OpenAIRepository, OpenAIUnavailableError

# Initial, tunable weighting — not a fixed law. See
# docs/infrastructure/confidence-calibration.md.
LOGPROB_WEIGHT = 0.7
EMBEDDING_WEIGHT = 0.3

# When the logprob signal alone is already this decisive (near-certain or
# near-zero), the embedding cross-check's independent semantic-sanity-check
# role has little left to add — a clear-cut case doesn't need a second
# opinion. Skipping it saves ~173ms average (measured,
# docs/infrastructure/latency-investigation.md) and 2 embedding API calls
# on exactly the responses least likely to need them.
DECISIVE_HIGH_THRESHOLD = 0.9
DECISIVE_LOW_THRESHOLD = 0.1


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


def _params_to_synthetic_sentence(params: dict[str, Any]) -> str:
    parts = []
    for field_name, value in params.items():
        if isinstance(value, dict):
            bounds = ", ".join(f"{bound_name}={bound_value}" for bound_name, bound_value in value.items())
            parts.append(f"{field_name}: {bounds}")
        elif isinstance(value, list):
            parts.append(f"{field_name}: {', '.join(str(item) for item in value)}")
        else:
            parts.append(f"{field_name}: {value}")
    return " ".join(parts)


def _cosine_similarity(vector_a: list[float], vector_b: list[float]) -> float:
    dot_product = sum(a * b for a, b in zip(vector_a, vector_b))
    norm_a = math.sqrt(sum(a * a for a in vector_a))
    norm_b = math.sqrt(sum(b * b for b in vector_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot_product / (norm_a * norm_b)


async def compute_embedding_similarity(canonical_query: str, params: dict[str, Any]) -> float:
    synthetic_sentence = _params_to_synthetic_sentence(params)
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
) -> float:
    logprob_confidence = compute_logprob_confidence(token_logprobs, present_field_names)

    if logprob_confidence >= DECISIVE_HIGH_THRESHOLD or logprob_confidence <= DECISIVE_LOW_THRESHOLD:
        return min(max(logprob_confidence, 0.0), 1.0)

    try:
        embedding_similarity = await compute_embedding_similarity(canonical_query, validated_params)
    except OpenAIUnavailableError:
        # A successful extraction shouldn't be thrown away because the
        # unrelated embedding cross-check couldn't run — fall back to the
        # logprob signal alone rather than failing the whole tier.
        embedding_similarity = logprob_confidence

    confidence = LOGPROB_WEIGHT * logprob_confidence + EMBEDDING_WEIGHT * embedding_similarity
    return min(max(confidence, 0.0), 1.0)
