"""Orchestrates the full pipeline: sanitize -> normalize -> cache lookup ->
classify+extract -> threshold check -> [LLM fallback] -> cache write ->
return. The only module that sequences the others — see
docs/decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md.
"""

import time
from dataclasses import dataclass

from app.config import settings
from app.logger import log_activity, log_metric
from app.metrics import PARSE_REQUEST_DURATION_SECONDS, PARSE_REQUESTS_TOTAL
from app.repositories.cache_repository import build_cache_key, cache_repository
from app.schema.responses import ParseResponse
from app.schema.taxonomy_models import VERTICAL_METRIC_LABELS
from app.services.classifier_service import classify_query
from app.services.extractor_service import extract_params
from app.services.llm_fallback_service import run_llm_fallback
from app.services.normalizer_service import normalize_query
from app.services.sanitizer_service import sanitize_query


@dataclass
class ParseResult:
    response: ParseResponse
    path: str  # "cache" | "rules" | "llm" — which tier resolved this request


@log_activity
async def parse_query(raw_query: str) -> ParseResult:
    started_at = time.perf_counter()

    sanitized_query = sanitize_query(raw_query)
    canonical_query = normalize_query(sanitized_query)
    cache_key = build_cache_key(canonical_query)

    cached_response = cache_repository.get(cache_key)
    if cached_response is not None:
        result = ParseResult(response=ParseResponse(**cached_response), path="cache")
        _record_metrics(result, started_at)
        return result

    classification = classify_query(canonical_query)
    rule_path_params = extract_params(classification.vertical, canonical_query, classification.term_occurrences)

    if classification.confidence >= settings.confidence_threshold:
        response = ParseResponse(
            category=classification.vertical,
            params=rule_path_params.model_dump(exclude_none=True),
            confidence=classification.confidence,
            notes=[],
        )
        log_metric(
            event="parse_decision",
            path="rules",
            vertical=classification.vertical.value,
            confidence=classification.confidence,
        )
        cache_repository.set(cache_key, response.model_dump(mode="json"))
        result = ParseResult(response=response, path="rules")
        _record_metrics(result, started_at)
        return result

    fallback_result = await run_llm_fallback(classification.vertical, canonical_query, rule_path_params)
    response = ParseResponse(
        category=classification.vertical,
        params=fallback_result.params.model_dump(exclude_none=True),
        confidence=fallback_result.confidence,
        notes=fallback_result.notes,
    )
    log_metric(
        event="parse_decision",
        path="llm",
        tier_used=fallback_result.tier_used,
        vertical=classification.vertical.value,
        rule_path_confidence=classification.confidence,
        confidence=fallback_result.confidence,
    )
    cache_repository.set(cache_key, response.model_dump(mode="json"))
    result = ParseResult(response=response, path="llm")
    _record_metrics(result, started_at)
    return result


def _record_metrics(result: ParseResult, started_at: float) -> None:
    duration_seconds = time.perf_counter() - started_at
    PARSE_REQUEST_DURATION_SECONDS.labels(path=result.path).observe(duration_seconds)
    PARSE_REQUESTS_TOTAL.labels(category=VERTICAL_METRIC_LABELS[result.response.category]).inc()
