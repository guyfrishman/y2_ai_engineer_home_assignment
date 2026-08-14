# Observability

## What's implemented ✅

**Structured JSON logging** via `log_event` (see
[`../conventions/logging.md`](../conventions/logging.md)). Every request
emits machine-parseable lines tagged with `trace_id` (set once per request
by middleware), so a single request is traceable across the call graph.
Security events carry a distinct `security_`-prefixed `event` tag,
greppable separately from ordinary parsing-decision logs.

**Prometheus metrics** at `GET /metrics`, two layers:

1. HTTP-level basics (request count/status/latency per route) from
   `prometheus-fastapi-instrumentator`, wired in `main.py`.
2. Custom pipeline metrics, `app/metrics.py`:

| Metric | Type | Labels | What it answers |
|---|---|---|---|
| `parse_requests_total` | Counter | `category` (ASCII: `real_estate`/`vehicles`/`used_goods`) | Total requests per category — a required metric |
| `parse_cache_result_total` | Counter | `result` (`hit`/`miss`) | Cache hit ratio — a required metric |
| `parse_model_calls_total` | Counter | `tier` (`tier1`/`tier2`), `outcome` (`success`/`validation_failed`/`api_error`) | Model call success/failure rate — a required metric |
| `parse_request_duration_seconds` | Histogram | `path` (`cache`/`rules`/`llm`/`coalesced`/`error`) | p50/p95 latency — **per path**, not blended, so an SLA violation in one tier can't hide under a healthy aggregate |
| `parse_tokens_total` | Counter | `model`, `token_type` (`prompt`/`completion`) | Token usage — a required metric |
| `parse_cost_usd_total` | Counter | `model` | $/request — a required metric, computed from a verified pricing table (see `../infrastructure/cost-model.md`) |
| `parse_errors_total` | Counter | — | Requests that raised mid-pipeline (see `parse_service.py`'s try/finally — latency is still recorded, under an `path="error"` bucket on the duration histogram, even when a request fails) |

Category labels are ASCII (`real_estate`, not `נדל״ן`) specifically for
Prometheus/PromQL/Grafana — the JSON API response still uses the Hebrew
taxonomy strings.

Error rate has two sources: the instrumentator's per-route status-code
histogram (non-2xx / total) for HTTP-level errors, `parse_errors_total`
for pipeline-level exceptions specifically, and
`parse_model_calls_total{outcome="api_error"}` for the LLM-specific case.

## What's not implemented, and why 🧭

**OpenTelemetry tracing** — explicitly out of scope at this depth. The
brief marks it "(Optional)," and the middleware-set `trace_id` already
answers the practical question ("which request failed, where in the call
graph") without adding a collector to run. If this service needed
distributed tracing across multiple hops, the `trace_id` already in every
log line is the join key OTel would use — the groundwork is there, just
not wired to an exporter.

**A snapshot/dashboard JSON**, as a Deliverable item — see the root
`README.md`'s Observability row for a captured `/metrics` snapshot from a
real loadtest run instead of a synthetic dashboard export.

## Health & SLOs

- `GET /health` reports `{"status": "ok", "taxonomy_version": "..."}` — a
  readiness signal that also surfaces which taxonomy build is loaded,
  useful when diagnosing a cache-key mismatch after a taxonomy update.
- SLO targets are the brief's own: p95 ≤150ms cache/rules path, p95 ≤600ms
  model path, ≥12 QPS per instance. `scripts/loadtest.py` measures and
  prints PASS/FAIL against exactly these numbers — see the root README for
  a real captured run.
