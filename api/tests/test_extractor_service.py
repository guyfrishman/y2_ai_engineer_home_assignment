from app.services.classifier_service import classify_query
from app.services.extractor_service import extract_params
from app.services.normalizer_service import normalize_query
from app.services.sanitizer_service import sanitize_query


def _parse_with_rules(raw_query: str) -> dict:
    canonical = normalize_query(sanitize_query(raw_query))
    classification = classify_query(canonical)
    params = extract_params(classification.vertical, canonical, classification.term_occurrences)
    return params.model_dump(exclude_none=True)


def test_real_estate_golden_example():
    params = _parse_with_rules("דירת 3 חדרים בירושלים עד מליון שח")
    assert params["עיר"] == "ירושלים"
    assert params["מס׳_חדרים"] == 3
    assert params["מחיר"] == {"max": 1000000}


def test_vehicle_golden_example_matches_brief_exactly():
    params = _parse_with_rules("טויוטה קורולה 2018-2021 עד 70 אלף שח צבע לבן")
    assert params == {
        "יצרן": "טויוטה",
        "דגם": "קורולה",
        "שנה": {"min": 2018, "max": 2021},
        "מחיר": {"max": 70000},
        "צבע": "לבן",
    }


def test_used_goods_golden_example_partial_extraction():
    # "iPhone" itself isn't a taxonomy term (only the brand "אפל" is), so
    # brand/model/sector go unextracted by rules alone — exactly the case
    # the LLM fallback exists for. The rule path should still get what it
    # honestly can.
    params = _parse_with_rules("אייפון 13 פרו 256 גיגה כחול כמו חדש עד 2500")
    assert params["מצב"] == "כמו חדש"
    assert params["נפח_אחסון"] == "256GB"
    assert params["צבע"] == "כחול"
    assert params["מחיר"] == {"max": 2500}


def test_price_range_between_phrase():
    params = _parse_with_rules("דירה בחיפה בין 500000 ל800000 שח")
    assert params["מחיר"] == {"min": 500000, "max": 800000}


def test_price_above_minimum():
    params = _parse_with_rules("וילה מעל 3 מיליון שח")
    assert params["מחיר"] == {"min": 3000000}


def test_vehicle_km_and_year_extraction_does_not_collide_with_price():
    params = _parse_with_rules("מאזדה CX-5 שנת 2019 100000 קמ עד 60000 שח")
    assert params["יצרן"] == "מאזדה"
    assert params["דגם"] == "CX-5"
    assert params["שנה"] == 2019
    assert params["ק״מ"] == 100000
    assert params["מחיר"] == {"max": 60000}


def test_real_estate_sale_with_amenities_resolves_via_rules():
    # docs/examples.md #2 — no cross-vertical term overlap, clears the
    # confidence threshold on rules alone.
    params = _parse_with_rules("דירה למכירה 4 חדרים קומה 3 עם מעלית וממ״ד")
    assert params == {
        "מצבי_עסקה": ["מכירה"],
        "סוגי_נכס": ["דירה"],
        "מס׳_חדרים": 4,
        "קומה": 3,
        "מעלית": True,
    }


def test_mazda_cx5_with_gearbox_and_fuel_resolves_via_rules():
    # docs/examples.md #4
    params = _parse_with_rules("מאזדה CX-5 שנת 2020 גיר אוטומטית דיזל")
    assert params == {
        "יצרן": "מאזדה",
        "דגם": "CX-5",
        "שנה": 2020,
        "תיבת_הילוכים": "אוטומטית",
        "סוג_דלק": "דיזל",
    }


def test_kia_sportage_typo_correction_resolves_via_rules():
    # docs/examples.md #5 — "ספורטאז" (missing geresh) and "קמ" (no
    # punctuation) both get corrected via the static typo map before
    # extraction.
    params = _parse_with_rules("קיה ספורטאז 2021 עד 150000 קמ")
    assert params == {
        "יצרן": "קיה",
        "דגם": "ספורטאז׳",
        "שנה": 2021,
        "ק״מ": 150000,
    }


def test_samsung_tv_resolves_via_rules():
    # docs/examples.md #7
    params = _parse_with_rules("סמסונג טלוויזיה 55 אינץ QLED")
    assert params == {"גודל_אינצ׳": 55, "טכנולוגיה": "QLED", "מותג": "סמסונג"}


def test_lenovo_laptop_partial_extraction():
    # docs/examples.md #8 — sector/subcategory go unextracted by rules,
    # same class of gap as the iPhone example.
    params = _parse_with_rules("מחשב נייד לנובו i7 16 גיגה זיכרון כמו חדש")
    assert params["מצב"] == "כמו חדש"
    assert params["מעבד"] == "i7"
    assert params["מותג"] == "לנובו"


def test_extracted_params_reject_fields_outside_taxonomy():
    # extract_params validates through the taxonomy Pydantic model, which
    # has extra="forbid" — there is no code path that can smuggle an
    # invented field into the response.
    params = _parse_with_rules("דירת 3 חדרים בירושלים")
    assert set(params.keys()).issubset(
        {
            "מצבי_עסקה", "סוגי_נכס", "עיר", "שכונה", "רחוב", "מס׳_חדרים", "קומה",
            "סה״כ_קומות", "מ״ר_בנוי", "מ״ר_מגרש", "מרפסות", "מרפסת_שמש", "מחיר",
            "תאריך_כניסה", "מצב_נכס", "מעלית", "חניה", "מחסן", "מיזוג", "ממ״ד",
            "גישה_לנכים", "בעלות", "בעלות_מקרקעין", "חיות_מחמד", "ריהוט",
            "כיווני_אוויר", "קרבה", "ארנונה_חודשית",
        }
    )
