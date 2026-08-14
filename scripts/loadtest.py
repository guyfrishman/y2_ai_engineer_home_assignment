"""Concurrent load test against a running /parse service.

Fires a mix of cache-hit, rules-path, and LLM-fallback-triggering queries
concurrently, measures p50/p95 latency per resolution path (read from the
X-Parse-Path response header) and overall QPS, and prints a PASS/FAIL table
against the brief's actual stated targets — not just raw numbers left for
the reader to compare themselves.

Usage (from the api/ directory, so the service's own dependencies resolve):
    cd api
    uv run python ../scripts/loadtest.py
    uv run python ../scripts/loadtest.py --base-url http://localhost:8000 --requests 300 --concurrency 30

Note: if OPENAI_API_KEY isn't configured on the server, "llm" path timings
reflect the fast api_error-then-degrade path, not a real model round trip —
this script detects and flags that case rather than silently reporting
misleadingly fast "llm" numbers as if they were real model latency.
"""

import argparse
import asyncio
import random
import time
from dataclasses import dataclass, field

import httpx

QPS_TARGET = 12.0
CACHE_RULES_P95_TARGET_SECONDS = 0.150
LLM_P95_TARGET_SECONDS = 0.600

# A repeated query (guaranteed cache hits after request #1), several
# distinct high-confidence queries (resolve via rules), and several
# low-confidence/ambiguous queries (trigger the LLM fallback tier).
CACHE_QUERY = "טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן"
RULES_PATH_QUERIES = [
    "מאזדה CX-5 שנת 2020",
    "יונדאי טוסון 2019 בעלות פרטי",
    "קיה ספורטאז' שנת 2021 גיר אוטומטית",
    "מרצדס C-Class 2018 דיזל",
    "טסלה Model 3 2022 חשמלי",
]
LLM_PATH_QUERIES = [
    "דירת 3 חדרים בירושלים עד מליון שח",
    "אייפון 13 פרו 256 גיגה כחול כמו חדש עד 2500",
    "וילה יפה עם גינה גדולה",
    "משהו נחמד למכירה בזול",
]


@dataclass
class RequestOutcome:
    path: str | None
    duration_seconds: float
    status_code: int


@dataclass
class LoadTestResults:
    outcomes: list[RequestOutcome] = field(default_factory=list)
    wall_clock_seconds: float = 0.0


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = min(int(len(sorted_values) * fraction), len(sorted_values) - 1)
    return sorted_values[index]


async def _send_one(client: httpx.AsyncClient, query: str, semaphore: asyncio.Semaphore) -> RequestOutcome:
    async with semaphore:
        started_at = time.perf_counter()
        try:
            response = await client.post("/parse", json={"q": query})
        except httpx.HTTPError:
            # A hard connection reset (e.g. uvicorn's --limit-concurrency
            # dropping a connection rather than returning a clean 503, seen
            # during the queueing investigation) is a real outcome to count,
            # not a reason to crash the whole run and lose every other
            # in-flight result.
            duration_seconds = time.perf_counter() - started_at
            return RequestOutcome(path=None, duration_seconds=duration_seconds, status_code=0)
        duration_seconds = time.perf_counter() - started_at
        return RequestOutcome(
            path=response.headers.get("x-parse-path"),
            duration_seconds=duration_seconds,
            status_code=response.status_code,
        )


def _build_query_plan(total_requests: int) -> list[str]:
    pool = [CACHE_QUERY] * 3 + RULES_PATH_QUERIES + LLM_PATH_QUERIES
    plan = []
    while len(plan) < total_requests:
        plan.extend(pool)
    return plan[:total_requests]


# "דירת" (construct form) never matches the taxonomy's "דירה" -- a documented
# confidence gap (see docs/infrastructure/confidence-calibration.md) that
# reliably keeps every generated variant below confidence_threshold regardless
# of room count/city/price (verified: 30/30 sampled below 0.58). Used only by
# --llm-ratio below, where a sustained target ratio needs many distinct
# LLM-triggering queries -- cycling the fixed 4-query LLM_PATH_QUERIES pool
# collapses into cache hits (and, with in-flight coalescing, a single shared
# call) after the first occurrence of each, so it can't sustain a requested
# ratio of real fallback traffic over a long run.
_LOW_CONFIDENCE_CITIES = ["ירושלים", "תל אביב", "חיפה", "באר שבע", "נתניה", "אשדוד", "ראשון לציון", "פתח תקווה"]
_LOW_CONFIDENCE_ROOM_COUNTS = [1, 2, 3, 4, 5]
_LOW_CONFIDENCE_PRICE_CEILINGS = [600000, 900000, 1200000, 1500000, 1800000]


def _generate_low_confidence_queries(count: int) -> list[str]:
    queries = []
    for i in range(count):
        city = _LOW_CONFIDENCE_CITIES[i % len(_LOW_CONFIDENCE_CITIES)]
        rooms = _LOW_CONFIDENCE_ROOM_COUNTS[i % len(_LOW_CONFIDENCE_ROOM_COUNTS)]
        price = _LOW_CONFIDENCE_PRICE_CEILINGS[i % len(_LOW_CONFIDENCE_PRICE_CEILINGS)]
        queries.append(f"דירת {rooms} חדרים ב{city} עד {price} שח")
    return queries


def _build_query_plan_with_llm_ratio(total_requests: int, llm_ratio: float) -> list[str]:
    """Same cache/rules/llm shape as _build_query_plan, but with the LLM-path
    share held at an explicit target ratio via distinct generated queries,
    for controlled concurrency-vs-ratio comparisons (see
    docs/infrastructure/latency-investigation.md's queueing-hypothesis
    section). Deterministically shuffled so repeated runs at the same
    parameters are comparable."""
    llm_count = round(total_requests * llm_ratio)
    other_count = total_requests - llm_count
    llm_queries = _generate_low_confidence_queries(llm_count)

    other_pool = [CACHE_QUERY] * 3 + RULES_PATH_QUERIES
    other_queries = []
    while len(other_queries) < other_count:
        other_queries.extend(other_pool)
    other_queries = other_queries[:other_count]

    combined = llm_queries + other_queries
    random.Random(42).shuffle(combined)
    return combined


async def run_loadtest(
    base_url: str, total_requests: int, concurrency: int, llm_ratio: float | None = None
) -> LoadTestResults:
    query_plan = (
        _build_query_plan_with_llm_ratio(total_requests, llm_ratio)
        if llm_ratio is not None
        else _build_query_plan(total_requests)
    )
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        started_at = time.perf_counter()
        outcomes = await asyncio.gather(*(_send_one(client, query, semaphore) for query in query_plan))
        wall_clock_seconds = time.perf_counter() - started_at

    return LoadTestResults(outcomes=list(outcomes), wall_clock_seconds=wall_clock_seconds)


def _print_report(results: LoadTestResults) -> None:
    successful = [outcome for outcome in results.outcomes if outcome.status_code == 200]
    failed_count = len(results.outcomes) - len(successful)
    qps = len(results.outcomes) / results.wall_clock_seconds if results.wall_clock_seconds > 0 else 0.0

    by_path: dict[str, list[float]] = {}
    for outcome in successful:
        by_path.setdefault(outcome.path or "unknown", []).append(outcome.duration_seconds)

    print(f"\nTotal requests: {len(results.outcomes)}  (failed: {failed_count})")
    if failed_count:
        failure_status_counts: dict[int, int] = {}
        for outcome in results.outcomes:
            if outcome.status_code != 200:
                failure_status_counts[outcome.status_code] = failure_status_counts.get(outcome.status_code, 0) + 1
        # status_code 0 = a transport-level failure (connection reset/refused)
        # rather than an HTTP response at all -- e.g. uvicorn's
        # --limit-concurrency dropping a connection instead of returning a
        # clean 503. Reported separately since it's a harsher failure mode
        # than a normal error status.
        breakdown = ", ".join(
            f"{'connection-reset' if code == 0 else code}: {count}" for code, count in sorted(failure_status_counts.items())
        )
        print(f"Failure breakdown: {breakdown}")
    print(f"Wall-clock time: {results.wall_clock_seconds:.2f}s")
    print(f"Measured QPS: {qps:.2f}\n")

    print(f"{'path':<10} {'count':>6} {'p50 (ms)':>10} {'p95 (ms)':>10}")
    for path, durations in sorted(by_path.items()):
        p50_ms = _percentile(durations, 0.50) * 1000
        p95_ms = _percentile(durations, 0.95) * 1000
        print(f"{path:<10} {len(durations):>6} {p50_ms:>10.1f} {p95_ms:>10.1f}")

    cache_and_rules_durations = by_path.get("cache", []) + by_path.get("rules", [])
    llm_durations = by_path.get("llm", [])
    cache_rules_p95 = _percentile(cache_and_rules_durations, 0.95)
    llm_p95 = _percentile(llm_durations, 0.95)

    print("\n--- PASS/FAIL against brief targets ---")
    _print_check("QPS >= 12", qps >= QPS_TARGET, f"{qps:.2f}")
    _print_check(
        "cache/rules path p95 <= 150ms",
        cache_and_rules_durations and cache_rules_p95 <= CACHE_RULES_P95_TARGET_SECONDS,
        f"{cache_rules_p95 * 1000:.1f}ms" if cache_and_rules_durations else "no cache/rules samples",
    )
    _print_check(
        "model (llm) path p95 <= 600ms",
        llm_durations and llm_p95 <= LLM_P95_TARGET_SECONDS,
        f"{llm_p95 * 1000:.1f}ms" if llm_durations else "no llm samples",
    )

    if llm_durations and _percentile(llm_durations, 0.50) < 0.05:
        print(
            "\nNOTE: llm-path p50 is under 50ms, which is far faster than a real "
            "model round trip — this almost certainly means OPENAI_API_KEY isn't "
            "configured on the server and requests are resolving via the fast "
            "api_error -> degrade path, not a real Tier 1/Tier 2 call. Re-run "
            "against a server with a real key for authoritative model-path numbers."
        )


def _print_check(label: str, passed: bool, measured: str) -> None:
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {label}  (measured: {measured})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument(
        "--llm-ratio",
        type=float,
        default=None,
        help="Hold the LLM-path share of traffic at this fraction (e.g. 0.5) using distinct "
        "generated queries instead of the fixed 8-query mix, for controlled ratio experiments.",
    )
    args = parser.parse_args()

    print(f"Running loadtest against {args.base_url} — {args.requests} requests, concurrency {args.concurrency}")
    results = asyncio.run(run_loadtest(args.base_url, args.requests, args.concurrency, args.llm_ratio))
    _print_report(results)


if __name__ == "__main__":
    main()
