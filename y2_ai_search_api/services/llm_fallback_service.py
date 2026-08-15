"""Single-tier LLM fallback, only invoked when the rule path's confidence is
below settings.confidence_threshold.

  Tier 1 (settings.openai_fallback_model) -> on any failure (api_error or
  validation failure) -> degrade to the rule path's own result.

See docs/DESIGN.md.
"""

import functools
import json
import re
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

# Fixed and categorically different from the measured tier below: with
# no successful generation from the tier, there is nothing to measure,
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
# The classify call succeeded and the model explicitly said "none of the
# three" -- not a failure, so this must not share DEGRADED_CONFIDENCE/
# CATEGORY_DEGRADED_NOTE (which mean "we tried and guessed").
NOT_APPLICABLE_CONFIDENCE = 0.0
NOT_APPLICABLE_NOTE = "query does not match any supported category (נדל״ן / רכב / יד_שנייה)"

# A full-schema, mostly-null response tops out around 190-230 completion
# tokens in measurement (see docs/DESIGN.md). This is a safety bound
# against a pathological runaway generation, not a tuning lever for the
# normal case — it's comfortably above anything a correct response needs.
MAX_FALLBACK_COMPLETION_TOKENS = 400


@dataclass
class LlmFallbackResult:
    params: BaseModel
    confidence: float
    tier_used: str  # "tier1" | "degraded"
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


@dataclass(frozen=True)
class TierCallResult:
    """failure_reason: None on success, "api_error", or "validation_failed"
    (invalid JSON or schema violation) -- run_llm_fallback degrades to the
    rule path's own result on either failure reason."""

    params: BaseModel | None
    token_logprobs: list | None
    llm_returned_fields: dict | None
    failure_reason: str | None = None


def _mask_claimed_numbers(text: str, rule_path_params: BaseModel) -> str:
    """Blank out digit sequences the rule path already attributed to a
    field, so the extraction call can't independently re-derive a
    *different* field from the same number. The rule extractor's own
    span-consumption already prevents this within a single rule-path pass
    (e.g. a room count and a price can't both claim the same digits) — but
    it only clears digits from its own working copy of the text, never the
    canonical_query the LLM is shown, so that protection stopped at the
    rule/LLM boundary. A price number with no other numeric cue nearby
    (e.g. "... 95000 ש״ח" with no "ק״מ" in sight) was observed getting
    claimed by the rule path as מחיר *and independently reinvented* by the
    LLM as ק״מ, since the model never learns Tier 1's a number is already
    spoken for. See docs/DESIGN.md.
    """
    numbers: set[str] = set()
    for value in rule_path_params.model_dump(exclude_none=True).values():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            numbers.add(str(int(value)) if float(value).is_integer() else str(value))
        elif isinstance(value, dict):
            for bound in value.values():
                if isinstance(bound, (int, float)) and not isinstance(bound, bool):
                    numbers.add(str(int(bound)) if float(bound).is_integer() else str(bound))
    if not numbers:
        return text
    pattern = re.compile(r"(?<!\d)(" + "|".join(re.escape(n) for n in numbers) + r")(?!\d)")
    return pattern.sub(lambda match: " " * len(match.group(0)), text)


async def _call_tier(
    vertical: Vertical, canonical_query: str, model_name: str, tier_label: str, rule_path_params: BaseModel
) -> TierCallResult:
    """llm_returned_fields is what the model itself generated, before
    merging in the rule path's already-known fields — the confidence calc
    scores the model's own answer, not fields it was never asked to
    produce."""
    model_class = taxonomy_repository.params_models[vertical]
    already_known_fields = frozenset(rule_path_params.model_dump(exclude_none=True).keys())
    scoped_schema = _scoped_strict_json_schema(model_class, already_known_fields)
    # Numbers the rule path already claimed are masked out here, not in
    # canonical_query itself: compute_llm_confidence's embedding cross-check
    # (called separately, below) still needs the full, unmasked query text.
    query_for_extraction = _mask_claimed_numbers(canonical_query, rule_path_params)
    messages = [
        {"role": "system", "content": build_extraction_system_prompt(vertical)},
        {"role": "user", "content": query_for_extraction},
    ]
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "search_params", "schema": scoped_schema, "strict": True},
    }
    log_event(level="DEBUG", event="llm_call_request", tier=tier_label, model=model_name, messages=messages)

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
        return TierCallResult(None, None, None, failure_reason="api_error")

    raw_content = response.choices[0].message.content or "{}"
    log_event(level="DEBUG", event="llm_call_response", tier=tier_label, raw_content=raw_content)
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
        return TierCallResult(None, None, None, failure_reason="validation_failed")

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
        return TierCallResult(None, None, None, failure_reason="validation_failed")

    log_event(event="llm_call_outcome", tier=tier_label, outcome="success", model=model_name, duration_ms=elapsed_ms())
    PARSE_MODEL_CALLS_TOTAL.labels(tier=tier_label, outcome="success").inc()
    token_logprobs = response.choices[0].logprobs.content if response.choices[0].logprobs else []
    return TierCallResult(validated_params, token_logprobs, llm_returned_fields)


# Nullable, same pattern every optional taxonomy field already uses.
# Members come from the Vertical enum, never hand-typed.
class _CategoryClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    קטגוריה: Literal[tuple(vertical.value for vertical in Vertical)] | None


_CATEGORY_FIELD_NAME = "קטגוריה"
MAX_CLASSIFICATION_COMPLETION_TOKENS = 20


@dataclass(frozen=True)
class ClassificationOutcome:
    """failed=True: the call itself errored -- degrade to the rule path's
    default. failed=False, vertical=None: model explicitly said "none of
    the three". failed=False, vertical=X: normal pick."""

    vertical: Vertical | None
    failed: bool

    @classmethod
    def api_failure(cls) -> "ClassificationOutcome":
        return cls(vertical=None, failed=True)


async def run_category_classification(canonical_query: str) -> ClassificationOutcome:
    """Classify-only call for zero-signal queries (classification.confidence
    == 0.0). Separate from extraction to avoid asking about all 3 verticals'
    combined fields before knowing which one applies."""
    schema = _strict_json_schema(_CategoryClassification)
    messages = [
        {"role": "system", "content": build_classification_system_prompt()},
        {"role": "user", "content": canonical_query},
    ]
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "query_category", "schema": schema, "strict": True},
    }
    log_event(level="DEBUG", event="llm_call_request", tier="classify", model=settings.openai_fallback_model, messages=messages)

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
        return ClassificationOutcome.api_failure()

    raw_content = response.choices[0].message.content or "{}"
    log_event(level="DEBUG", event="llm_call_response", tier="classify", raw_content=raw_content)
    try:
        parsed = json.loads(raw_content)
        raw_category = parsed[_CATEGORY_FIELD_NAME]
        vertical = Vertical(raw_category) if raw_category is not None else None
    except (json.JSONDecodeError, KeyError, ValueError):
        log_event(
            event="security_llm_validation_failed",
            tier="classify",
            outcome="validation_failed",
            reason="invalid_category",
            duration_ms=elapsed_ms(),
        )
        PARSE_MODEL_CALLS_TOTAL.labels(tier="classify", outcome="validation_failed").inc()
        return ClassificationOutcome.api_failure()

    log_event(
        event="llm_call_outcome",
        tier="classify",
        outcome="success",
        model=settings.openai_fallback_model,
        vertical=vertical.value if vertical is not None else None,
        duration_ms=elapsed_ms(),
    )
    PARSE_MODEL_CALLS_TOTAL.labels(tier="classify", outcome="success").inc()
    return ClassificationOutcome(vertical=vertical, failed=False)


async def run_llm_fallback(
    vertical: Vertical, canonical_query: str, rule_path_params: BaseModel
) -> LlmFallbackResult:
    tier1 = await _call_tier(vertical, canonical_query, settings.openai_fallback_model, "tier1", rule_path_params)
    if tier1.params is not None:
        confidence = await compute_llm_confidence(
            canonical_query,
            tier1.token_logprobs,
            list(tier1.llm_returned_fields.keys()),
            tier1.params.model_dump(exclude_none=True),
            vertical,
        )
        return LlmFallbackResult(params=tier1.params, confidence=confidence, tier_used="tier1")

    return LlmFallbackResult(
        params=rule_path_params,
        confidence=DEGRADED_CONFIDENCE,
        tier_used="degraded",
        notes=[DEGRADED_NOTE],
    )
