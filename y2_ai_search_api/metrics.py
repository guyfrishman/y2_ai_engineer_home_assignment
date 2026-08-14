"""Custom Prometheus metrics for the parse pipeline. HTTP-level basics
(request count/status/latency) come from prometheus-fastapi-instrumentator,
wired in main.py — these cover what that instrumentation can't see: which
vertical, whether the cache absorbed the request, which LLM tier ran and
how it went, and token/cost accounting.
"""

from prometheus_client import Counter, Histogram

PARSE_REQUESTS_TOTAL = Counter(
    "parse_requests_total", "Total /parse requests by resolved category", ["category"]
)

PARSE_CACHE_RESULT_TOTAL = Counter(
    "parse_cache_result_total", "Cache lookup outcomes", ["result"]  # hit | miss
)

PARSE_ERRORS_TOTAL = Counter(
    "parse_errors_total", "Requests that raised mid-pipeline, including QueryRejectedError (routed to a 400)"
)

PARSE_MODEL_CALLS_TOTAL = Counter(
    "parse_model_calls_total",
    "LLM fallback calls by tier and outcome",
    ["tier", "outcome"],  # tier: tier1 | tier2 ; outcome: success | validation_failed | api_error
)

# Separate histograms per path, not one blended histogram — blending would
# hide an SLA violation in one tier (e.g. the LLM path creeping past 600ms)
# under a healthy-looking aggregate average.
PARSE_REQUEST_DURATION_SECONDS = Histogram(
    "parse_request_duration_seconds", "Request latency by resolution path", ["path"]  # cache | rules | llm | coalesced | error
)

PARSE_TOKENS_TOTAL = Counter(
    "parse_tokens_total", "Token usage by model and token type", ["model", "token_type"]  # prompt | completion
)

PARSE_COST_USD_TOTAL = Counter(
    "parse_cost_usd_total", "Estimated USD cost by model", ["model"]
)

# Verified against developers.openai.com/api/docs/pricing (Aug 2026), USD
# per 1M tokens. Embeddings have no "completion" side, priced at 0.
# gpt-4.1-nano/mini, not gpt-5-nano/mini: the entire gpt-5 family rejects
# logprobs requests (verified live, 403), which this service's confidence
# score depends on — see docs/DESIGN.md.
MODEL_PRICING_USD_PER_MILLION_TOKENS: dict[str, dict[str, float]] = {
    "gpt-4.1-nano": {"prompt": 0.10, "completion": 0.40},
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60},
    "gpt-4.1-mini": {"prompt": 0.40, "completion": 1.60},
    "text-embedding-3-small": {"prompt": 0.02, "completion": 0.0},
}


def record_token_usage_and_cost(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    PARSE_TOKENS_TOTAL.labels(model=model, token_type="prompt").inc(prompt_tokens)
    PARSE_TOKENS_TOTAL.labels(model=model, token_type="completion").inc(completion_tokens)

    pricing = MODEL_PRICING_USD_PER_MILLION_TOKENS.get(model)
    if pricing is None:
        return
    cost_usd = (prompt_tokens * pricing["prompt"] + completion_tokens * pricing["completion"]) / 1_000_000
    PARSE_COST_USD_TOTAL.labels(model=model).inc(cost_usd)
