"""Two-tier LLM fallback, only invoked when the rule path's confidence is
below settings.confidence_threshold.

  Tier 1 (cheap, settings.openai_fallback_model) -> on required-field
  validation failure or api_error -> Tier 2 (stronger,
  settings.openai_escalation_model) -> on the same failure modes ->
  degrade to the rule path's own result.

No Tier 3. See docs/DESIGN.md.
"""

import functools
import json
import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from config import settings
from logger import log_event
from metrics import PARSE_MODEL_CALLS_TOTAL
from prompts.system_prompts import build_classification_system_prompt, build_extraction_system_prompt
from repositories.openai_repository import OpenAIRepository, OpenAIUnavailableError
from repositories.taxonomy_repository import taxonomy_repository
from schema.taxonomy_models import Vertical
from services.llm_confidence_service import compute_llm_confidence

# Fixed and categorically different from the two measured tiers below: with
# no successful generation from either tier, there is nothing to measure,
# so this is an honest fixed "unknown," not a measurement.
DEGRADED_CONFIDENCE = 0.15
DEGRADED_NOTE = "low-confidence extraction — model fallback did not produce a valid result"
# A genuinely different failure mode from DEGRADED_NOTE: the *category*
# itself was unresolved (zero taxonomy/cue-word signal, and the
# classify-only call also failed), not just the extracted fields on an
# otherwise-known category.
CATEGORY_DEGRADED_NOTE = (
    "low-confidence extraction — the category itself was low-confidence (zero taxonomy signal), "
    "not just the extracted fields"
)

# A full-schema, mostly-null response tops out around 190-230 completion
# tokens in measurement (see docs/DESIGN.md). This is a safety bound
# against a pathological runaway generation, not a tuning lever for the
# normal case — it's comfortably above anything a correct response needs.
MAX_FALLBACK_COMPLETION_TOKENS = 400


@dataclass
class LlmFallbackResult:
    params: BaseModel
    confidence: float
    tier_used: str  # "tier1" | "tier2" | "degraded"
    notes: list[str] = field(default_factory=list)


@functools.lru_cache(maxsize=8)
def _strict_json_schema(model_class: type[BaseModel]) -> dict:
    """Build an OpenAI Structured-Outputs strict-mode schema from a taxonomy
    params model: every object needs additionalProperties=False and every
    property listed in required (nullable types carry the "optional" meaning
    instead, since every field in our models is Optional already).

    Cached per model class (there are only 3 — one per vertical, each a
    stable, reused class built once at TaxonomyRepository construction) —
    the derivation is deterministic, so there's no reason to rebuild an
    ~3.5KB dict from scratch on every fallback call. The schema's actual
    JSON content was already verified byte-identical across rebuilds
    before this cache was added; this only removes wasted CPU work, it
    doesn't change what's sent over the wire.
    """
    schema = model_class.model_json_schema()
    defs = schema.get("$defs", {})
    _force_strict_recursive(schema, defs)
    for def_schema in defs.values():
        _force_strict_recursive(def_schema, defs)
    return schema


def _force_strict_recursive(node: dict, defs: dict) -> None:
    # "default" is a pydantic/Python-side concern (what value to use when a
    # field is omitted) — not part of what OpenAI's strict mode expects a
    # schema to declare, and stricter implementations have been known to
    # reject unrecognized keywords, so it's dropped defensively rather than
    # left in and only found to be a problem against the live API.
    node.pop("default", None)
    if "properties" in node:
        node["additionalProperties"] = False
        node["required"] = list(node["properties"].keys())
        for property_schema in node["properties"].values():
            _force_strict_ref_or_node(property_schema, defs)
    if "items" in node:
        _force_strict_ref_or_node(node["items"], defs)
    for combinator_key in ("anyOf", "oneOf", "allOf"):
        for sub_schema in node.get(combinator_key, []):
            _force_strict_ref_or_node(sub_schema, defs)


def _force_strict_ref_or_node(node: dict, defs: dict) -> None:
    if "$ref" in node:
        return  # already handled by iterating defs.values() directly
    _force_strict_recursive(node, defs)


def _scoped_strict_json_schema(model_class: type[BaseModel], already_known_fields: frozenset[str]) -> dict:
    """Same strict schema as _strict_json_schema, restricted to the
    top-level fields the rule path didn't already fill — measured to cut
    ~8% completion tokens and ~12% latency with no validation cost (see
    docs/DESIGN.md's Latency section). The LLM is only asked to fill gaps
    rules couldn't; already-known fields are merged back in after the call
    in _call_tier, and the merged result
    is validated against the FULL, unscoped model there — this narrows what
    the model is asked to produce, never what's allowed through validation.

    Not cached (already_known_fields varies per request, unlike
    _strict_json_schema's fixed one-per-vertical inputs) — built by
    filtering a copy of the cached full schema, so it stays cheap. Falls
    back to the full schema if every field is already known (nothing left
    to scope down to) rather than sending OpenAI an empty properties dict.
    """
    full_schema = _strict_json_schema(model_class)
    scoped_properties = {
        name: spec for name, spec in full_schema["properties"].items() if name not in already_known_fields
    }
    if not scoped_properties:
        return full_schema
    return {
        **full_schema,
        "properties": scoped_properties,
        "required": [name for name in full_schema["required"] if name not in already_known_fields],
    }


async def _call_tier(
    vertical: Vertical, canonical_query: str, model_name: str, tier_label: str, rule_path_params: BaseModel
) -> tuple[BaseModel, list, dict] | tuple[None, None, None]:
    """Returns (validated_params, token_logprobs, llm_returned_fields) on
    success, or (None, None, None) on api_error or required-field
    validation failure — both logged as security_llm_validation_failed so
    they're greppable separately from normal request logs.

    llm_returned_fields is what the model itself generated, before merging
    in the rule path's already-known fields — the confidence calc scores
    the model's own answer, not fields it was never asked to produce."""
    model_class = taxonomy_repository.params_models[vertical]
    already_known_fields = frozenset(rule_path_params.model_dump(exclude_none=True).keys())
    scoped_schema = _scoped_strict_json_schema(model_class, already_known_fields)
    messages = [
        {"role": "system", "content": build_extraction_system_prompt(vertical)},
        {"role": "user", "content": canonical_query},
    ]
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "search_params", "schema": scoped_schema, "strict": True},
    }

    started_at = time.perf_counter()

    def elapsed_ms() -> float:
        # Own timer, not just OpenAIRepository.chat's own duration_ms log --
        # that one's tagged "llm_call_outcome" with no tier, this one is
        # tagged tier=tier_label, so "how long did Tier 1 take" is readable
        # off one line instead of correlating two adjacent log entries.
        return round((time.perf_counter() - started_at) * 1000, 1)

    try:
        response = await OpenAIRepository.chat(
            messages,
            model=model_name,
            response_format=response_format,
            logprobs=True,
            max_completion_tokens=MAX_FALLBACK_COMPLETION_TOKENS,
        )
    except OpenAIUnavailableError as error:
        log_event(
            event="security_llm_validation_failed",
            tier=tier_label,
            outcome="api_error",
            reason=str(error),
            duration_ms=elapsed_ms(),
        )
        PARSE_MODEL_CALLS_TOTAL.labels(tier=tier_label, outcome="api_error").inc()
        return None, None, None

    raw_content = response.choices[0].message.content or "{}"
    try:
        llm_returned_fields = json.loads(raw_content)
    except json.JSONDecodeError:
        log_event(
            event="security_llm_validation_failed",
            tier=tier_label,
            outcome="validation_failed",
            reason="invalid_json",
            duration_ms=elapsed_ms(),
        )
        PARSE_MODEL_CALLS_TOTAL.labels(tier=tier_label, outcome="validation_failed").inc()
        return None, None, None

    # Validation always runs against the FULL, unscoped taxonomy model, on
    # the merged result — a field the LLM was never even asked about still
    # has to satisfy every constraint a model-derived field would. Scoping
    # narrows what's asked for on the wire; it never narrows what's
    # accepted. See tests/test_llm_fallback_service.py's
    # test_scoped_schema_validates_the_merged_result_against_the_full_model.
    merged_fields = {**rule_path_params.model_dump(exclude_none=True), **llm_returned_fields}
    try:
        validated_params = model_class(**merged_fields)
    except ValidationError as error:
        log_event(
            event="security_llm_validation_failed",
            tier=tier_label,
            outcome="validation_failed",
            reason=str(error)[:200],
            duration_ms=elapsed_ms(),
        )
        PARSE_MODEL_CALLS_TOTAL.labels(tier=tier_label, outcome="validation_failed").inc()
        return None, None, None

    log_event(event="llm_call_outcome", tier=tier_label, outcome="success", model=model_name, duration_ms=elapsed_ms())
    PARSE_MODEL_CALLS_TOTAL.labels(tier=tier_label, outcome="success").inc()
    token_logprobs = response.choices[0].logprobs.content if response.choices[0].logprobs else []
    return validated_params, token_logprobs, llm_returned_fields


# Routing only, not extraction: a single required field, so this never
# needs logprobs (no confidence to measure off tokens) and stays far cheaper
# than a full extraction call. Members come straight from the Vertical enum
# — never a hand-typed list of category names to drift out of sync with it.
class _CategoryClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    קטגוריה: Literal[tuple(vertical.value for vertical in Vertical)]


_CATEGORY_FIELD_NAME = "קטגוריה"
# One enum value plus JSON punctuation — comfortably bounds a pathological
# runaway generation without ever constraining a normal answer.
MAX_CLASSIFICATION_COMPLETION_TOKENS = 20


async def run_category_classification(canonical_query: str) -> Vertical | None:
    """Tiny Structured-Outputs call used only when the rule path found zero
    taxonomy/cue-word evidence for every vertical (classification.confidence
    == 0.0 in parse_service._resolve) — at that point classify_query's own
    "winning" vertical is just Vertical's first-declared member via max()'s
    tie-break, not a real pick. Deliberately a separate, single-field call
    rather than folding category into the per-vertical extraction schema:
    that would mean asking about all 3 verticals' ~60+ combined fields
    before even knowing which one applies, reintroducing the exact
    output-volume latency problem schema-scoping was adopted to avoid.

    Returns None on api_error or a malformed/invalid response — the caller
    degrades to the rule path's own (defaulted) result rather than trusting
    a category that isn't real either way.
    """
    schema = _strict_json_schema(_CategoryClassification)
    messages = [
        {"role": "system", "content": build_classification_system_prompt()},
        {"role": "user", "content": canonical_query},
    ]
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "query_category", "schema": schema, "strict": True},
    }

    started_at = time.perf_counter()

    def elapsed_ms() -> float:
        return round((time.perf_counter() - started_at) * 1000, 1)

    try:
        response = await OpenAIRepository.chat(
            messages,
            model=settings.openai_fallback_model,
            response_format=response_format,
            max_completion_tokens=MAX_CLASSIFICATION_COMPLETION_TOKENS,
        )
    except OpenAIUnavailableError as error:
        log_event(
            event="security_llm_validation_failed",
            tier="classify",
            outcome="api_error",
            reason=str(error),
            duration_ms=elapsed_ms(),
        )
        PARSE_MODEL_CALLS_TOTAL.labels(tier="classify", outcome="api_error").inc()
        return None

    raw_content = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw_content)
        vertical = Vertical(parsed[_CATEGORY_FIELD_NAME])
    except (json.JSONDecodeError, KeyError, ValueError):
        log_event(
            event="security_llm_validation_failed",
            tier="classify",
            outcome="validation_failed",
            reason="invalid_category",
            duration_ms=elapsed_ms(),
        )
        PARSE_MODEL_CALLS_TOTAL.labels(tier="classify", outcome="validation_failed").inc()
        return None

    log_event(
        event="llm_call_outcome",
        tier="classify",
        outcome="success",
        model=settings.openai_fallback_model,
        vertical=vertical.value,
        duration_ms=elapsed_ms(),
    )
    PARSE_MODEL_CALLS_TOTAL.labels(tier="classify", outcome="success").inc()
    return vertical


async def run_llm_fallback(
    vertical: Vertical, canonical_query: str, rule_path_params: BaseModel
) -> LlmFallbackResult:
    tier1_params, tier1_logprobs, tier1_llm_fields = await _call_tier(
        vertical, canonical_query, settings.openai_fallback_model, "tier1", rule_path_params
    )
    if tier1_params is not None:
        confidence = await compute_llm_confidence(
            canonical_query,
            tier1_logprobs,
            list(tier1_llm_fields.keys()),
            tier1_params.model_dump(exclude_none=True),
            vertical,
        )
        return LlmFallbackResult(params=tier1_params, confidence=confidence, tier_used="tier1")

    tier2_params, tier2_logprobs, tier2_llm_fields = await _call_tier(
        vertical, canonical_query, settings.openai_escalation_model, "tier2", rule_path_params
    )
    if tier2_params is not None:
        confidence = await compute_llm_confidence(
            canonical_query,
            tier2_logprobs,
            list(tier2_llm_fields.keys()),
            tier2_params.model_dump(exclude_none=True),
            vertical,
        )
        return LlmFallbackResult(params=tier2_params, confidence=confidence, tier_used="tier2")

    return LlmFallbackResult(
        params=rule_path_params,
        confidence=DEGRADED_CONFIDENCE,
        tier_used="degraded",
        notes=[DEGRADED_NOTE],
    )
