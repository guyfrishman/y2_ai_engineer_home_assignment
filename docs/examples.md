# Worked examples

Eight realistic Hebrew queries across all three verticals, with the actual
JSON the service returns. Five resolve via the rule path alone (no model
call); three fall below `confidence_threshold` (0.58) and route to the LLM
fallback tier — for those, the `params` shown is what the **rule path alone**
extracts (verified, deterministic), with a note on what the LLM tier adds.
All rule-path outputs here are asserted by `y2_ai_search_api/tests/test_extractor_service.py`
and `y2_ai_search_api/tests/test_security_redteam.py` — nothing below is aspirational.

## נדל״ן (Real Estate)

### 1. `דירת 3 חדרים בירושלים עד מליון שח` — LLM path (rule confidence 0.5)

Rule path extracts:
```json
{
  "category": "נדל״ן",
  "params": { "עיר": "ירושלים", "מס׳_חדרים": 3, "מחיר": { "max": 1000000 } },
  "confidence": 0.5,
  "notes": []
}
```
Below threshold because "דירת" (construct form) doesn't match the taxonomy's
"דירה", so `סוגי_נכס` goes unextracted — exactly the gap the LLM fallback
closes. Verified with a real `OPENAI_API_KEY`, Tier 1 (`gpt-4.1-nano`)
resolves this with confidence 0.8074:
```json
{
  "category": "נדל״ן",
  "params": {
    "מצבי_עסקה": ["מכירה"],
    "עיר": "ירושלים",
    "מס׳_חדרים": 3,
    "מחיר": { "max": 1000000 }
  },
  "confidence": 0.8074,
  "notes": []
}
```
It inferred `מצבי_עסקה: ["מכירה"]` (for sale) — a reasonable default for a
price-ceiling search with no explicit "להשכרה"/"מכירה" — rather than adding
`סוגי_נכס`, showing that real LLM output, unlike the deterministic rule
path, varies call to call and isn't always exactly the field a human might
predict in advance.

### 2. `דירה למכירה 4 חדרים קומה 3 עם מעלית וממ״ד` — rules path (confidence 0.7875)

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
  "confidence": 0.7875,
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

### 4. `מאזדה CX-5 שנת 2020 גיר אוטומטית דיזל` — rules path (confidence 0.8571)

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
  "confidence": 0.8571,
  "notes": []
}
```
No cross-vertical term overlap (unlike colors or city names), so this
clears the threshold with a large margin — a good illustration of the
cache/rules-path SLA's target case.

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

### 6. `אייפון 13 פרו 256 גיגה כחול כמו חדש עד 2500` — LLM path (rule confidence 0.5556)

Rule path extracts:
```json
{
  "category": "יד_שנייה",
  "params": {
    "מצב": "כמו חדש",
    "נפח_אחסון": "256GB",
    "צבע": "כחול",
    "מחיר": { "max": 2500 }
  },
  "confidence": 0.5556,
  "notes": []
}
```
"אייפון" isn't a taxonomy term — only the brand "אפל" is — so `סקטור`,
`תת_קטגוריה`, `מותג`, and `דגם` go unextracted by rules alone. This is the
textbook LLM-fallback case: recognizing "iPhone" implies Apple/cellular
phones requires product knowledge no taxonomy lookup provides.

Verified with a real `OPENAI_API_KEY`, Tier 1 (`gpt-4.1-nano`) actually
returned:
```json
{
  "category": "יד_שנייה",
  "params": { "מחיר": { "max": 2500 }, "אחסון_GB": 256 },
  "confidence": 0.8225,
  "notes": []
}
```
Worth being honest about: this is a **real, observed limitation**, not the
brief's own expected output for this query. The model used `אחסון_GB`
(laptop storage — a valid field in the allowlist, since `UsedGoodsParams`
unions fields across all subcategories) instead of `נפח_אחסון` (phone
storage), and dropped `מצב`/`צבע`/`סקטור`/`תת_קטגוריה` entirely, despite a
high confidence score. An individually-allowlisted field chosen for the
wrong subcategory is a gap the cross-field `סקטור`/`תת_קטגוריה` validator
(added after a similar real finding — see
[ADR 0001](../decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md))
doesn't close, since it only checks that those two fields are mutually
consistent, not that every other field name is contextually appropriate.
Documented as a known limitation, not silently hidden.

### 7. `סמסונג טלוויזיה 55 אינץ QLED` — rules path (confidence 0.6)

```json
{
  "category": "יד_שנייה",
  "params": { "גודל_אינצ׳": 55, "טכנולוגיה": "QLED", "מותג": "סמסונג" },
  "confidence": 0.6,
  "notes": []
}
```

### 8. `מחשב נייד לנובו i7 16 גיגה זיכרון כמו חדש` — LLM path (rule confidence 0.5556)

Rule path extracts:
```json
{
  "category": "יד_שנייה",
  "params": { "מצב": "כמו חדש", "מעבד": "i7", "מותג": "לנובו" },
  "confidence": 0.5556,
  "notes": []
}
```
`סקטור`/`תת_קטגוריה` (אלקטרוניקה/מחשבים_ניידים) and RAM size go unextracted
by rules — again, exactly the gap the LLM tier is there to close.

Verified with a real `OPENAI_API_KEY`, Tier 1 returned:
```json
{
  "category": "יד_שנייה",
  "params": { "מעבד": "i7", "זיכרון_RAM": 16, "מותג": "לנובו" },
  "confidence": 0.8437,
  "notes": []
}
```
This is the query that originally surfaced the sector/subcategory
cross-field bug documented in example 6 and
[ADR 0001](../decisions/0001-hybrid-rule-first-llm-fallback-pipeline.md):
an earlier real call for this same query returned `סקטור: "מוסיקה_וכלים"`
(music) paired with `תת_קטגוריה: "מחשבים_ניידים"` (laptops) — a nonsensical
combination that passed per-field validation since both values are
individually valid. The result above is post-fix, from the same live
model; it happened to omit `סקטור`/`תת_קטגוריה` entirely this time rather
than risk another mismatch — real model output varies call to call.

## Reproducing these

```bash
cd y2_ai_search_api
uv run pytest tests/test_extractor_service.py -v
```

Or against a running instance:
```bash
curl -X POST http://localhost:8000/parse -H "Content-Type: application/json" \
  --data-binary '{"q":"טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן"}'
```
