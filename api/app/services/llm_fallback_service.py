"""Two-tier LLM fallback, only invoked when the rule path's confidence is
below settings.confidence_threshold.

  Tier 1 (cheap, settings.openai_fallback_model) -> on required-field
  validation failure or api_error -> Tier 2 (stronger,
  settings.openai_escalation_model) -> on the same failure modes ->
  degrade to the rule path's own result.

No Tier 3. See docs/decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md.
"""

import functools
import json
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from app.config import settings
from app.logger import log_activity, log_metric
from app.metrics import PARSE_MODEL_CALLS_TOTAL
from app.prompts.system_prompts import build_extraction_system_prompt
from app.repositories.openai_repository import OpenAIRepository, OpenAIUnavailableError
from app.repositories.taxonomy_repository import taxonomy_repository
from app.schema.taxonomy_models import Vertical
from app.services.llm_confidence_service import compute_llm_confidence

# Fixed and categorically different from the two measured tiers below: with
# no successful generation from either tier, there is nothing to measure,
# so this is an honest fixed "unknown," not a measurement.
DEGRADED_CONFIDENCE = 0.15
DEGRADED_NOTE = "low-confidence extraction — model fallback did not produce a valid result"


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


async def _call_tier(
    vertical: Vertical, canonical_query: str, model_name: str, tier_label: str
) -> tuple[BaseModel, list] | tuple[None, None]:
    """Returns (validated_params, token_logprobs) on success, or (None, None)
    on api_error or required-field validation failure — both logged as
    security_llm_validation_failed so they're greppable separately from
    normal request logs."""
    model_class = taxonomy_repository.params_models[vertical]
    messages = [
        {"role": "system", "content": build_extraction_system_prompt(vertical)},
        {"role": "user", "content": canonical_query},
    ]
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "search_params", "schema": _strict_json_schema(model_class), "strict": True},
    }

    try:
        response = await OpenAIRepository.chat(
            messages, model=model_name, response_format=response_format, logprobs=True
        )
    except OpenAIUnavailableError as error:
        log_metric(event="security_llm_validation_failed", tier=tier_label, outcome="api_error", reason=str(error))
        PARSE_MODEL_CALLS_TOTAL.labels(tier=tier_label, outcome="api_error").inc()
        return None, None

    raw_content = response.choices[0].message.content or "{}"
    try:
        raw_params = json.loads(raw_content)
    except json.JSONDecodeError:
        log_metric(event="security_llm_validation_failed", tier=tier_label, outcome="validation_failed", reason="invalid_json")
        PARSE_MODEL_CALLS_TOTAL.labels(tier=tier_label, outcome="validation_failed").inc()
        return None, None

    try:
        validated_params = model_class(**raw_params)
    except ValidationError as error:
        log_metric(
            event="security_llm_validation_failed",
            tier=tier_label,
            outcome="validation_failed",
            reason=str(error)[:200],
        )
        PARSE_MODEL_CALLS_TOTAL.labels(tier=tier_label, outcome="validation_failed").inc()
        return None, None

    log_metric(event="llm_call_outcome", tier=tier_label, outcome="success", model=model_name)
    PARSE_MODEL_CALLS_TOTAL.labels(tier=tier_label, outcome="success").inc()
    token_logprobs = response.choices[0].logprobs.content if response.choices[0].logprobs else []
    return validated_params, token_logprobs


@log_activity
async def run_llm_fallback(
    vertical: Vertical, canonical_query: str, rule_path_params: BaseModel
) -> LlmFallbackResult:
    tier1_params, tier1_logprobs = await _call_tier(
        vertical, canonical_query, settings.openai_fallback_model, "tier1"
    )
    if tier1_params is not None:
        present_fields = list(tier1_params.model_dump(exclude_none=True).keys())
        confidence = await compute_llm_confidence(
            canonical_query, tier1_logprobs, present_fields, tier1_params.model_dump(exclude_none=True)
        )
        return LlmFallbackResult(params=tier1_params, confidence=confidence, tier_used="tier1")

    tier2_params, tier2_logprobs = await _call_tier(
        vertical, canonical_query, settings.openai_escalation_model, "tier2"
    )
    if tier2_params is not None:
        present_fields = list(tier2_params.model_dump(exclude_none=True).keys())
        confidence = await compute_llm_confidence(
            canonical_query, tier2_logprobs, present_fields, tier2_params.model_dump(exclude_none=True)
        )
        return LlmFallbackResult(params=tier2_params, confidence=confidence, tier_used="tier2")

    return LlmFallbackResult(
        params=rule_path_params,
        confidence=DEGRADED_CONFIDENCE,
        tier_used="degraded",
        notes=[DEGRADED_NOTE],
    )
