"""Orchestrates the full pipeline: sanitize -> normalize -> cache lookup ->
in-flight coalescing -> classify+extract -> threshold check -> [LLM
fallback] -> cache write -> return. The only module that sequences the
others — see docs/DESIGN.md.
"""

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
    path: str  # "cache" | "coalesced" | "rules" | "llm" — how this request was resolved


# Cache-key -> in-flight resolution, for requests currently past the cache
# check but not yet cached. Without this, N identical requests arriving
# concurrently (a newly-popular query fanning out under real Zipfian
# traffic) all miss the cache and all pay for their own LLM call — a
# stampede. Only ever mutated at points with no `await` in between (see
# parse_query), so plain dict operations are safe on the single-threaded
# event loop without a lock.
_in_flight_requests: dict[str, asyncio.Future["ParseResult"]] = {}


async def parse_query(raw_query: str) -> ParseResult:
    started_at = time.perf_counter()
    result: ParseResult | None = None
    canonical_query: str | None = None  # may stay None if sanitize_query itself raises
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
            # Another concurrent request for this exact canonical query is
            # already resolving — wait for its result instead of redoing
            # the work (which, below the confidence threshold, means
            # paying for a second LLM call for no reason).
            resolved = await existing_future
            result = ParseResult(response=resolved.response, path="coalesced")
            return result

        future: asyncio.Future[ParseResult] = asyncio.get_running_loop().create_future()
        _in_flight_requests[cache_key] = future
        try:
            resolved = await _resolve(canonical_query, cache_key)
        except BaseException as error:
            # BaseException, not Exception: asyncio.CancelledError is a
            # BaseException (not an Exception, since Python 3.8), and this
            # resolving request's own task is exactly what a graceful
            # shutdown cancels once the grace period elapses. If that
            # cancellation isn't caught here, the future is never settled
            # -- any concurrent coalesced waiter (`await existing_future`
            # above) hangs forever, since nothing else ever resolves it.
            # Confirmed directly, not assumed: reproduced this hang before
            # adding this handler.
            if not future.done():
                # A bare CancelledError propagating into an unrelated
                # waiter's task is surprising (asyncio treats it as that
                # task's own cancellation, not a normal catchable error) --
                # wrap it so coalesced waiters see a plain, catchable
                # failure instead. The resolving task's own cancellation
                # semantics are unaffected: the original `error` is what
                # gets re-raised below, not this wrapped copy.
                waiter_error = error if isinstance(error, Exception) else RuntimeError(f"in-flight resolution did not complete: {error!r}")
                future.set_exception(waiter_error)
                future.exception()  # mark retrieved so a future nobody else awaited doesn't warn on GC
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
        # Always runs — a request that raises mid-pipeline still gets its
        # latency recorded (under an "error" path label) and the request
        # that succeeded gets its normal per-path/category accounting. A
        # bare `return` inside the try above still triggers this block
        # before the function actually returns.
        duration_seconds = time.perf_counter() - started_at
        if result is not None:
            PARSE_REQUEST_DURATION_SECONDS.labels(path=result.path).observe(duration_seconds)
            PARSE_REQUESTS_TOTAL.labels(category=VERTICAL_METRIC_LABELS[result.response.category]).inc()
            # The one place the query text is logged, once — not per
            # decorated function as the old @log_activity design did
            # (~10x/request across the call chain, a real volume and
            # privacy problem for what is, after all, user search input).
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
    """Run the rule/LLM pipeline for a canonical query that's neither
    cached nor already being resolved by a concurrent request. Only ever
    called once per distinct in-flight query — parse_query's in-flight
    future tracking coalesces concurrent duplicates onto this one call.
    """
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
        # Exactly 0.0, not "below some threshold": this is the precise,
        # already-proven case of zero taxonomy-term/cue-word evidence for
        # every vertical (see classifier_service.classify_query) -- at this
        # point `classification.vertical` is Vertical's first-declared
        # member via max()'s tie-break, not a real pick, and handing it to
        # run_llm_fallback as if it were one is exactly the bug this branch
        # exists to close. Anything above 0.0 is real partial signal and
        # keeps going through the unmodified path below, using
        # classification.vertical as a genuine hint.
        llm_vertical = await run_category_classification(canonical_query)
        if llm_vertical is None:
            # The classify-only call itself failed -- a different, and
            # worse, failure mode than a shaky extraction: the category is
            # unknown, not just the fields. Degrade honestly to the rule
            # path's own (defaulted) result rather than pretend it's real.
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
        # Occurrences are empty by construction here -- confidence == 0.0
        # means no vertical (including the newly-classified one) had any
        # taxonomy-term or cue-word match, so there's nothing beyond
        # regex-only fields (price, year, ...) for the extractor to fill.
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
