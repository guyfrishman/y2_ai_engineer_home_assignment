# Yad2 - AI Engineer Home Assignment

## Goal
Design and implement a free-text search understanding service that converts a Hebrew query into structured Yad2 search parameters.

Your service should:

- Detect which marketplace vertical the query refers to:  
  - נדל״ן (Real Estate)  
  - רכב (Vehicles)  
  - יד_שנייה (Second-hand)  
- Extract and normalize the relevant structured filters for that vertical (e.g., city, rooms, price range, brand/model/specs).  
- Return a strict JSON response with the extracted parameters.  
- Be scalable, observable, cost-efficient, and secure (especially against prompt-injection).  

---

## Environment & Constraints
- **Language/Runtime:** Python  
- **Run mode:** Dockerized service running locally (provide Dockerfile)  
- **Input language:** Hebrew only, with tolerance to typos and slang  
- **LLM usage:** Any approach is allowed (rules, models, embeddings, hybrids).  
  - Must justify your choices and optimize for cost at Yad2 scale (tens of millions of searches/month).  
  - External embeddings/vector DBs are allowed.  
- **Secrets:** Use environment variables/config for API keys, never hardcode secrets.  

---

## API

### `POST /parse`
**Request body:**
```json
{ "q": "<free-text in Hebrew>" }
```

**Response body:**
```json
{
  "category": "נדל״ן | רכב | יד_שנייה",
  "params": { /* normalized filters per taxonomy */ },
  "confidence": 0.0,
  "notes": ["<optional normalization or assumption notes>"]
}
```

### `GET /health`
Returns simple health status.

### `GET /metrics`
Exposes observability metrics (Prometheus style is fine).

---

## Output Contract (Schema)
- **category** – enum: "נדל״ן", "רכב", "יד_שנייה"  
- **params** – object containing only allowed fields per taxonomy (`yad2_search_taxonomy_heavy.json`)  
  - Numeric ranges: `{ "min": <num>, "max": <num> }`  
  - Strings must be normalized (e.g., `"ירושלים" → "ירושליים"`)  
- **confidence** – float [0,1]  
- **notes** – optional array of strings with assumptions/normalizations  

Reject/flag unknown fields. Do not invent keys outside the taxonomy.  

---

## Non-Functional Requirements

### Scale & Performance
- Latency targets:  
  - p95 ≤ 600ms (model path)  
  - p95 ≤ 150ms (cache/rules only)  
- Throughput: ≥ 12 QPS per instance (~1M/day).  
- Caching: propose & implement (popular queries, partial-parse, normalization).  

### Cost Efficiency
- Track & expose token usage and cost/request.  
- Provide monthly cost estimate for **10M queries/month**.  
- Suggest cost reduction options (classifier + selective calls, embeddings+rules, prompt compression, caching).  

### Observability
Export metrics:
- Total requests / per category  
- Error rate  
- p50/p95 latency  
- Cache hit ratio  
- Token usage & cost/request  
- Model call success/failure rate  
- (Optional) Tracing with OpenTelemetry  

Use structured logs for parsing decisions & security events.  

### Security
- Fixed system prompts & allowlisted fields/categories.  
- Strict JSON Schema validation.  
- Input sanitization (strip emojis/control chars, normalize units).  
- Red-team tests for prompt-injection and abuse (long inputs, unicode tricks, slang, injection attempts).  

---

## Data & Taxonomy
Use the provided Hebrew taxonomy file: **`Yad2_search_taxonomy.json`**  
It includes fields for:  
- **נדל״ן:** transaction types, property types, city/rooms/size/floor ranges, amenities, typo mappings.  
- **רכב:** vehicle types, manufacturers → models, year/km/fuel/gearbox, safety features, typo mappings.  
- **יד_שנייה:** sectors, sub-categories, brand/spec fields, condition, price, location, typo mappings.  
- Global cleanup rules (units, typos, ranges).  

---

## Examples

### Example 1
**Query:**  
`דירת 3 חדרים בירושלים עד מליון שח`

**Response:**  
```json
{
  "category": "נדל״ן",
  "params": {
    "סוגי_נכס": ["דירה"],
    "מס׳_חדרים": 3,
    "עיר": "ירושלים",
    "מחיר": { "min": 0, "max": 1000000 }
  }
}
```

### Example 2
**Query:**  
`טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן`

**Response:**  
```json
{
  "category": "רכב",
  "params": {
    "יצרן": "טויוטה",
    "דגם": "קורולה",
    "שנה": { "min": 2018, "max": 2021 },
    "מחיר": { "max": 70000 },
    "צבע": "לבן"
  }
}
```

### Example 3
**Query:**  
`אייפון 13 פרו 256 ג׳יגה כחול כמו חדש עד 2500`

**Response:**  
```json
{
  "category": "יד_שנייה",
  "params": {
    "סקטור": "אלקטרוניקה",
    "תת_קטגוריה": "טלפונים_סלולריים",
    "מותג": "אפל",
    "דגם": "iPhone 13 Pro",
    "נפח_אחסון": "256GB",
    "צבע": "כחול",
    "מצב": "כמו חדש",
    "מחיר": { "max": 2500 }
  }
}
```

---

## Deliverables
1. Code (Python) + Dockerfile.  
2. API docs (OpenAPI/Swagger or README).  
3. Design doc (README section): architecture, scaling plan, caching, cost model, rationale for chosen methods.  
4. Observability: `/metrics` endpoint with required metrics; provide snapshot/dashboard JSON.  
5. Security tests: unit/integration covering prompt-injection & invalid inputs.  
6. Example I/O: 5–10 realistic Hebrew examples with expected JSON.  

---

## Evaluation Criteria
- **Correctness & Robustness:** classification, extraction, normalization, schema adherence.  
- **Design Quality:** trade-offs for latency, cost, scale; caching strategy; modular architecture.  
- **Observability & Security:** useful metrics/logs/traces; prompt-injection defenses.  
- **Code Quality:** readability, tests, Dockerization, documentation.  
- **Pragmatism:** credible path to very low cost per query at Yad2 scale.  
