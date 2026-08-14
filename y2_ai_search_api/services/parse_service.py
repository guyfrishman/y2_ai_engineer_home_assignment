import asyncio
import time
from dataclasses import dataclass
from config import settings
from logger import log_event
from metrics import PARSE_ERRORS_TOTAL, PARSE_REQUEST_DURATION_SECONDS, PARSE_REQUESTS_TOTAL
from repositories.cache_repository import build_cache_key, cache_repository
from schema.responses import ParseResponse
from schema.taxonomy_models import VERTICAL_METRIC_LABELS
from services.classifier_service import classify_query
from services.extractor_service import extract_params
from services.llm_fallback_service import (
    CATEGORY_DEGRADED_NOTE,
    DEGRADED_CONFIDENCE,
    run_category_classification,
    run_llm_fallback,
)
from services.normalizer_service import normalize_query
from services.sanitizer_service import sanitize_query


@dataclass
class ParseResult:
    response: ParseResponse
    path: str

_in_flight_requests: dict[str, asyncio.Future["ParseResult"]] = {}


async def parse_query(raw_query: str) -> ParseResult:
    started_at = time.perf_counter()
    result: ParseResult | None = None
    canonical_query: str | None = None
    try:
        sanitized_query = sanitize_query(raw_query)
        canonical_query = normalize_query(sanitized_query)
        cache_key = build_cache_key(canonical_query)

        cached_response = cache_repository.get(cache_key)
        if cached_response is not None:
            result = ParseResult(response=ParseResponse(**cached_response), path="cache")
            return result

        existing_future = _in_flight_requests.get(cache_key)
        if existing_future is not None:
            resolved = await existing_future
            result = ParseResult(response=resolved.response, path="coalesced")
            return result

        future: asyncio.Future[ParseResult] = asyncio.get_running_loop().create_future()
        _in_flight_requests[cache_key] = future

        try:
            resolved = await _resolve(canonical_query, cache_key)
        except BaseException as error:
            if not future.done():
                waiter_error = error if isinstance(error, Exception) else RuntimeError(f"in-flight resolution did not complete: {error!r}")
                future.set_exception(waiter_error)
                future.exception()
            raise
        else:
            future.set_result(resolved)
            result = resolved
            return result
        finally:
            _in_flight_requests.pop(cache_key, None)
    except Exception:
        PARSE_ERRORS_TOTAL.inc()
        raise
    finally:
        duration_seconds = time.perf_counter() - started_at
        if result is not None:
            PARSE_REQUEST_DURATION_SECONDS.labels(path=result.path).observe(duration_seconds)
            PARSE_REQUESTS_TOTAL.labels(category=VERTICAL_METRIC_LABELS[result.response.category]).inc()
            log_event(
                event="request_completed",
                path=result.path,
                latency_ms=round(duration_seconds * 1000, 1),
                category=result.response.category.value,
                confidence=result.response.confidence,
                query=canonical_query,
            )
        else:
            PARSE_REQUEST_DURATION_SECONDS.labels(path="error").observe(duration_seconds)


async def _resolve(canonical_query: str, cache_key: str) -> ParseResult:
    classification = classify_query(canonical_query)
    rule_path_params = extract_params(classification.vertical, canonical_query, classification.term_occurrences)

    if classification.confidence >= settings.confidence_threshold:
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
        cache_repository.set(cache_key, response.model_dump(mode="json"))
        return ParseResult(response=response, path="rules")

    vertical = classification.vertical
    if classification.confidence == 0.0:
        llm_vertical = await run_category_classification(canonical_query)
        if llm_vertical is None:
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
            cache_repository.set(cache_key, response.model_dump(mode="json"))
            return ParseResult(response=response, path="llm")

        vertical = llm_vertical
        rule_path_params = extract_params(vertical, canonical_query, [])

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
    cache_repository.set(cache_key, response.model_dump(mode="json"))
    return ParseResult(response=response, path="llm")
