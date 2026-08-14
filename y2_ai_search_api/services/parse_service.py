import time
from dataclasses import dataclass

from pydantic import BaseModel

from config import settings
from logger import log_event
from metrics import PARSE_ERRORS_TOTAL, PARSE_REQUEST_DURATION_SECONDS, PARSE_REQUESTS_TOTAL
from repositories.cache_repository import build_cache_key, cache_repository
from schema.responses import ParseResponse
from schema.taxonomy_models import VERTICAL_METRIC_LABELS, Vertical
from services.classifier_service import ClassificationResult, classify_query
from services.extractor_service import extract_params
from services.llm_fallback_service import (
    CATEGORY_DEGRADED_NOTE,
    DEGRADED_CONFIDENCE,
    NOT_APPLICABLE_CONFIDENCE,
    NOT_APPLICABLE_NOTE,
    run_category_classification,
    run_llm_fallback,
)
from services.normalizer_service import normalize_query
from services.sanitizer_service import sanitize_query

NULL_CATEGORY_LABEL = "null"  # metric/log label for category=None responses


@dataclass
class ParseResult:
    response: ParseResponse
    path: str  # "cache" | "rules" | "llm" | "null"


async def parse_query(raw_query: str) -> ParseResult:
    # No in-flight request coalescing: only dedupes within one process, not
    # across replicas -- not worth the Future/CancelledError complexity for
    # a guarantee that's incomplete by construction. A real fix needs a
    # distributed primitive (Redis SETNX + pub/sub); out of scope.
    started_at = time.perf_counter()
    result: ParseResult | None = None
    canonical_query: str | None = None
    try:
        sanitized_query = sanitize_query(raw_query)
        log_event(level="DEBUG", event="sanitize_query", input=raw_query, output=sanitized_query)
        canonical_query = normalize_query(sanitized_query)
        log_event(level="DEBUG", event="normalize_query", input=sanitized_query, output=canonical_query)
        cache_key = build_cache_key(canonical_query)

        cached_response = cache_repository.get(cache_key)
        if cached_response is not None:
            result = ParseResult(response=ParseResponse(**cached_response), path="cache")
            return result

        result = await _classify_and_resolve(canonical_query, cache_key)
        return result
    except Exception:
        PARSE_ERRORS_TOTAL.inc()
        raise
    finally:
        duration_seconds = time.perf_counter() - started_at
        if result is not None:
            category = result.response.category
            category_label = VERTICAL_METRIC_LABELS[category] if category is not None else NULL_CATEGORY_LABEL
            PARSE_REQUEST_DURATION_SECONDS.labels(path=result.path).observe(duration_seconds)
            PARSE_REQUESTS_TOTAL.labels(category=category_label).inc()
            log_event(
                event="request_completed",
                path=result.path,
                latency_ms=round(duration_seconds * 1000, 1),
                category=category.value if category is not None else None,
                confidence=result.response.confidence,
                params=result.response.params,
                query=canonical_query,
            )
        else:
            PARSE_REQUEST_DURATION_SECONDS.labels(path="error").observe(duration_seconds)


def _cache_and_return(cache_key: str, response: ParseResponse, path: str) -> ParseResult:
    cache_repository.set(cache_key, response.model_dump(mode="json"))
    return ParseResult(response=response, path=path)


async def _classify_and_resolve(canonical_query: str, cache_key: str) -> ParseResult:
    """Rule-path classify+extract, then rules if confidence clears
    threshold, otherwise the LLM branch."""
    classification = classify_query(canonical_query)
    log_event(
        level="DEBUG",
        event="classify_query",
        vertical=classification.vertical.value,
        confidence=classification.confidence,
        term_occurrences=[occurrence.matched_text for occurrence in classification.term_occurrences],
    )
    rule_path_params = extract_params(classification.vertical, canonical_query, classification.term_occurrences)
    log_event(level="DEBUG", event="extract_params", params=rule_path_params.model_dump(exclude_none=True))

    if classification.confidence >= settings.confidence_threshold:
        return _resolve_via_rules(classification, rule_path_params, cache_key)

    return await _run_llm_branch(classification, rule_path_params, canonical_query, cache_key)


def _resolve_via_rules(
    classification: ClassificationResult, rule_path_params: BaseModel, cache_key: str
) -> ParseResult:
    response = ParseResponse(
        category=classification.vertical,
        params=rule_path_params.model_dump(exclude_none=True),
        confidence=classification.confidence,
        notes=[],
    )
    log_event(
        event="parse_decision",
        path="rules",
        vertical=classification.vertical.value,
        confidence=classification.confidence,
    )
    return _cache_and_return(cache_key, response, "rules")


async def _run_llm_branch(
    classification: ClassificationResult, rule_path_params: BaseModel, canonical_query: str, cache_key: str
) -> ParseResult:
    """Below-threshold rule confidence. Zero signal (== 0.0) resolves the
    category via a classify-only call first; partial signal keeps
    classification.vertical as a hint straight into the extraction cascade."""
    vertical = classification.vertical
    if classification.confidence == 0.0:
        outcome = await run_category_classification(canonical_query)
        if outcome.failed:
            return _degrade_to_rule_path_default(classification, rule_path_params, cache_key)
        if outcome.vertical is None:
            return _not_applicable_result(classification, cache_key)
        vertical = outcome.vertical
        rule_path_params = extract_params(vertical, canonical_query, [])
        log_event(level="DEBUG", event="extract_params", vertical=vertical.value, params=rule_path_params.model_dump(exclude_none=True))

    return await _run_extraction_cascade(vertical, classification, rule_path_params, canonical_query, cache_key)


def _degrade_to_rule_path_default(
    classification: ClassificationResult, rule_path_params: BaseModel, cache_key: str
) -> ParseResult:
    response = ParseResponse(
        category=classification.vertical,
        params=rule_path_params.model_dump(exclude_none=True),
        confidence=DEGRADED_CONFIDENCE,
        notes=[CATEGORY_DEGRADED_NOTE],
    )
    log_event(
        event="parse_decision",
        path="llm",
        outcome="category_degraded",
        vertical=classification.vertical.value,
        rule_path_confidence=classification.confidence,
        confidence=DEGRADED_CONFIDENCE,
    )
    return _cache_and_return(cache_key, response, "llm")


def _not_applicable_result(classification: ClassificationResult, cache_key: str) -> ParseResult:
    response = ParseResponse(
        category=None,
        params={},
        confidence=NOT_APPLICABLE_CONFIDENCE,
        notes=[NOT_APPLICABLE_NOTE],
    )
    log_event(
        event="parse_decision",
        path="null",
        outcome="not_applicable",
        rule_path_confidence=classification.confidence,
        confidence=NOT_APPLICABLE_CONFIDENCE,
    )
    return _cache_and_return(cache_key, response, "null")


async def _run_extraction_cascade(
    vertical: Vertical,
    classification: ClassificationResult,
    rule_path_params: BaseModel,
    canonical_query: str,
    cache_key: str,
) -> ParseResult:
    fallback_result = await run_llm_fallback(vertical, canonical_query, rule_path_params)
    response = ParseResponse(
        category=vertical,
        params=fallback_result.params.model_dump(exclude_none=True),
        confidence=fallback_result.confidence,
        notes=fallback_result.notes,
    )
    log_event(
        event="parse_decision",
        path="llm",
        tier_used=fallback_result.tier_used,
        vertical=vertical.value,
        rule_path_confidence=classification.confidence,
        confidence=fallback_result.confidence,
    )
    return _cache_and_return(cache_key, response, "llm")
