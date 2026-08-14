# Worked examples

Nine realistic Hebrew queries across all three verticals, with the actual
JSON the service returns. All nine are verified against the running code —
`y2_ai_search_api/tests/test_extractor_service.py`,
`test_taxonomy_generated_classification.py`, and `test_zero_signal_classification.py`
assert the rule-path outputs; example 9's LLM-path numbers are a live,
freshly captured `OPENAI_API_KEY` run, not simulated.

## נדל״ן (Real Estate)

### 1. `דירת 3 חדרים בירושלים עד מליון שח` — rules path (confidence 0.6944)

```json
{
  "category": "נדל״ן",
  "params": { "עיר": "ירושלים", "מס׳_חדרים": 3, "מחיר": { "max": 1000000 } },
  "confidence": 0.6944,
  "notes": []
}
```

### 2. `דירה למכירה 4 חדרים קומה 3 עם מעלית וממ״ד` — rules path (confidence 0.875)

```json
{
  "category": "נדל״ן",
  "params": {
    "מצבי_עסקה": ["מכירה"],
    "סוגי_נכס": ["דירה"],
    "מס׳_חדרים": 4,
    "קומה": 3,
    "מעלית": true
  },
  "confidence": 0.875,
  "notes": []
}
```

## רכב (Vehicles)

### 3. `טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן` — rules path (confidence 0.5952)

```json
{
  "category": "רכב",
  "params": {
    "יצרן": "טויוטה",
    "דגם": "קורולה",
    "שנה": { "min": 2018, "max": 2021 },
    "מחיר": { "max": 70000 },
    "צבע": "לבן"
  },
  "confidence": 0.5952,
  "notes": []
}
```
Matches the assignment brief's own worked example exactly.

### 4. `מאזדה CX-5 שנת 2020 גיר אוטומטית דיזל` — rules path (confidence 0.625)

```json
{
  "category": "רכב",
  "params": {
    "יצרן": "מאזדה",
    "דגם": "CX-5",
    "שנה": 2020,
    "תיבת_הילוכים": "אוטומטית",
    "סוג_דלק": "דיזל"
  },
  "confidence": 0.625,
  "notes": []
}
```

### 5. `קיה ספורטאז 2021 עד 150000 קמ` — rules path (confidence 1.0)

```json
{
  "category": "רכב",
  "params": {
    "יצרן": "קיה",
    "דגם": "ספורטאז׳",
    "שנה": 2021,
    "ק״מ": 150000
  },
  "confidence": 1.0,
  "notes": []
}
```
Demonstrates typo correction: "ספורטאז" (missing the trailing geresh) is
corrected to "ספורטאז׳" via the taxonomy's static typo map, and "קמ" (no
punctuation) is corrected to "ק״מ" before extraction.

## יד_שנייה (Second-hand)

### 6. `אייפון 13 פרו 256 גיגה כחול כמו חדש עד 2500` — rules path (confidence 0.6806)

```json
{
  "category": "יד_שנייה",
  "params": {
    "מצב": "כמו חדש",
    "נפח_אחסון": "256GB",
    "צבע": "כחול",
    "מחיר": { "max": 2500 }
  },
  "confidence": 0.6806,
  "notes": []
}
```
"אייפון" isn't a taxonomy term — only the brand "אפל" is — so `סקטור`,
`תת_קטגוריה`, `מותג`, and `דגם` go unextracted by rules. Clears the
confidence threshold anyway on the fields it does get.

### 7. `סמסונג טלוויזיה 55 אינץ QLED` — rules path (confidence 0.6)

```json
{
  "category": "יד_שנייה",
  "params": { "גודל_אינצ׳": 55, "טכנולוגיה": "QLED", "מותג": "סמסונג" },
  "confidence": 0.6,
  "notes": []
}
```

### 8. `מחשב נייד לנובו i7 16 גיגה זיכרון כמו חדש` — rules path (confidence 0.6667)

```json
{
  "category": "יד_שנייה",
  "params": { "מצב": "כמו חדש", "מעבד": "i7", "מותג": "לנובו" },
  "confidence": 0.6667,
  "notes": []
}
```
`סקטור`/`תת_קטגוריה` (אלקטרוניקה/מחשבים_ניידים) and RAM size go unextracted
by rules — a partial, honest result, still above threshold.

## LLM fallback + zero-signal classification

### 9. `ג'יפ קטן עד 20 אש''ח` — LLM path (rule confidence 0.0)

The bug-fix case this pass exists for. "ג'יפ" isn't a taxonomy term or
cue word, and "20 אש''ח" (thousand-shekel abbreviation, doubled ASCII
apostrophe) is unrecognized by the rule path — zero taxonomy/cue-word
evidence for every vertical. `classification.confidence == 0.0` routes to
a dedicated classify-only LLM call (not the ordinary extraction fallback)
before any extraction is attempted:

```json
{
  "category": "רכב",
  "params": { "סוגי_רכב": ["רכב שטח"], "מחיר": { "max": 20000 } },
  "confidence": 0.827,
  "notes": []
}
```

Correctly classified as רכב (vehicles), not the נדל״ן default a bare
`max()` tie-break used to silently produce. Three real model calls: a tiny
classify-only call (286 prompt / 8 completion tokens), Tier 1 extraction
(2,995 prompt / 152 completion tokens), and the category-aware embedding
cross-check (2 calls, 21 + 46 tokens) — see `docs/DESIGN.md` for the cost
and latency this adds.

## Reproducing these

```bash
cd y2_ai_search_api
uv run pytest tests/test_extractor_service.py tests/test_taxonomy_generated_classification.py -v
```

Example 9 needs a real `OPENAI_API_KEY` (`tests/test_zero_signal_classification.py`
covers the same path with a mocked model instead, no key required) — the
query's apostrophes need shell-appropriate escaping, so it's easiest to
reproduce via the test suite or a JSON file rather than an inline `curl`
one-liner.

Against the rule path only, no key needed:
```bash
curl -X POST http://localhost:8000/parse -H "Content-Type: application/json" \
  --data-binary '{"q":"טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן"}'
```
