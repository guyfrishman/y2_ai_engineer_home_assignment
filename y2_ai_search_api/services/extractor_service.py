"""Per-vertical, dict-driven extraction of structured params from a
canonical query, built on top of the term occurrences ``classifier_service``
already found. Regex extraction for numeric fields consumes matched spans
as it goes, so later fields (e.g. price) don't re-claim a number already
attributed to an earlier one (e.g. rooms)."""

import re
from typing import Any

from pydantic import BaseModel, ValidationError

from repositories.taxonomy_repository import taxonomy_repository
from schema.taxonomy_models import Vertical
from services.classifier_service import TermOccurrence

_SQM_UNIT_PATTERN = r"(?:מ\"ר|מ״ר|מטרים רבועים|מטר מרובע|מ'|מטרים|מטר)"
_CURRENCY_UNIT_PATTERN = r"ש״ח"


def _collect_field_values(occurrences: list[TermOccurrence], field_name: str) -> list[str]:
    values: list[str] = []
    for occurrence in occurrences:
        if occurrence.field_name == field_name and occurrence.matched_text not in values:
            values.append(occurrence.matched_text)
    return values


def _first_field_value(occurrences: list[TermOccurrence], field_name: str) -> str | None:
    values = _collect_field_values(occurrences, field_name)
    return values[0] if values else None


def _contains_keyword(text: str, field_name: str) -> bool:
    """True if the field's own name (underscores read as spaces) appears
    literally in the text — used for boolean amenity fields, where the
    field name usually *is* the natural word a query would use."""
    keyword = field_name.replace("_", " ")
    return re.search(rf"(?<!\S){re.escape(keyword)}(?!\S)", text) is not None


def _extract_and_consume(text: str, pattern: str) -> tuple[str, int | float | None]:
    """Regex-extract a single number and blank out the matched span, so a
    later, broader pattern (e.g. price) can't re-claim the same digits."""
    match = re.search(pattern, text)
    if not match:
        return text, None
    value = float(match.group(1))
    if value.is_integer():
        value = int(value)
    consumed_text = text[: match.start()] + (" " * (match.end() - match.start())) + text[match.end() :]
    return consumed_text, value


def _extract_bounded_number(
    text: str, unit_pattern: str = "", require_unit_for_scalar: bool = True
) -> dict[str, int | float] | int | float | None:
    """Recognize "X-Y[unit]" as a range, "עד X[unit]" as a max-only range,
    "מעל X[unit]" as a min-only range, or (only when a unit is present, to
    avoid misattributing an unrelated bare number) "X[unit]" as a scalar."""
    suffix = rf"\s*{unit_pattern}" if unit_pattern else ""

    range_match = re.search(rf"(\d+)\s*-\s*(\d+){suffix}", text)
    if range_match:
        return {"min": int(range_match.group(1)), "max": int(range_match.group(2))}

    max_match = re.search(rf"עד\s+(\d+){suffix}", text)
    if max_match:
        return {"max": int(max_match.group(1))}

    min_match = re.search(rf"מעל\s+(\d+){suffix}", text)
    if min_match:
        return {"min": int(min_match.group(1))}

    if unit_pattern or not require_unit_for_scalar:
        scalar_match = re.search(rf"(\d+){suffix}", text)
        if scalar_match:
            return int(scalar_match.group(1))

    return None


def _extract_price(working_text: str) -> dict[str, int | float] | int | float | None:
    price = _extract_bounded_number(working_text, _CURRENCY_UNIT_PATTERN)
    if price is not None:
        return price
    # No currency word left in the text — fall back to any remaining
    # bounded phrase (עד/מעל/range). A bare, unqualified number is left
    # alone here: by this point every other numeric field (rooms, sqm,
    # year, km, ...) has already consumed its own match, so what's left is
    # ambiguous enough that guessing price from it would be more noise than
    # signal.
    return _extract_bounded_number(working_text, "", require_unit_for_scalar=True)


_YEAR_RANGE_PATTERN = re.compile(r"(19\d{2}|20\d{2})\s*-\s*(19\d{2}|20\d{2})")
_YEAR_SCALAR_PATTERN = re.compile(r"(?<!\S)(19\d{2}|20\d{2})(?!\S)")


def _extract_year(text: str) -> tuple[str, dict[str, int] | int | None]:
    range_match = _YEAR_RANGE_PATTERN.search(text)
    if range_match:
        consumed = text[: range_match.start()] + " " * len(range_match.group(0)) + text[range_match.end() :]
        return consumed, {"min": int(range_match.group(1)), "max": int(range_match.group(2))}
    scalar_match = _YEAR_SCALAR_PATTERN.search(text)
    if scalar_match:
        consumed = text[: scalar_match.start()] + " " * len(scalar_match.group(0)) + text[scalar_match.end() :]
        return consumed, int(scalar_match.group(1))
    return text, None


def _validate(model_class: type[BaseModel], raw_params: dict[str, Any]) -> BaseModel:
    try:
        return model_class(**raw_params)
    except ValidationError:
        # A field regex produced something the taxonomy's own bounds/type
        # rules reject (e.g. an out-of-range year). Drop down to whatever
        # subset of fields does validate, rather than failing extraction
        # outright — a partial rule-path result is still useful input to
        # the confidence score, which will route to the LLM if too little
        # survived.
        valid_params = {}
        for key, value in raw_params.items():
            try:
                valid_params[key] = value
                model_class(**valid_params)
            except ValidationError:
                valid_params.pop(key)
        return model_class(**valid_params)


REAL_ESTATE_BOOLEAN_FIELDS = (
    "מרפסת_שמש", "מעלית", "מחסן", "מיזוג", "ממ״ד", "גישה_לנכים", "חיות_מחמד",
)
REAL_ESTATE_ENUM_FIELDS = (
    "עיר", "שכונה", "מצב_נכס", "בעלות", "בעלות_מקרקעין", "ריהוט", "כיווני_אוויר", "קרבה",
)


def extract_real_estate_params(canonical_query: str, occurrences: list[TermOccurrence]) -> BaseModel:
    params: dict[str, Any] = {}
    working_text = canonical_query

    property_types = _collect_field_values(occurrences, "סוגי_נכס")
    if property_types:
        params["סוגי_נכס"] = property_types
    transaction_types = _collect_field_values(occurrences, "מצבי_עסקה")
    if transaction_types:
        params["מצבי_עסקה"] = transaction_types

    for field_name in REAL_ESTATE_ENUM_FIELDS:
        value = _first_field_value(occurrences, field_name)
        if value:
            params[field_name] = value

    for field_name in REAL_ESTATE_BOOLEAN_FIELDS:
        if _contains_keyword(canonical_query, field_name):
            params[field_name] = True

    working_text, rooms = _extract_and_consume(working_text, r"(\d+(?:\.\d+)?)\s*(?:חדרים|חדר)")
    if rooms is not None:
        params["מס׳_חדרים"] = rooms

    working_text, floor = _extract_and_consume(working_text, r"קומה\s*(\d+)")
    if floor is not None:
        params["קומה"] = floor

    working_text, sqm = _extract_and_consume(working_text, rf"(\d+)\s*{_SQM_UNIT_PATTERN}")
    if sqm is not None:
        params["מ״ר_בנוי"] = sqm

    price = _extract_price(working_text)
    if price is not None:
        params["מחיר"] = price

    return _validate(taxonomy_repository.params_models[Vertical.REAL_ESTATE], params)


def extract_vehicle_params(canonical_query: str, occurrences: list[TermOccurrence]) -> BaseModel:
    params: dict[str, Any] = {}
    working_text = canonical_query

    vehicle_types = _collect_field_values(occurrences, "סוגי_רכב")
    if vehicle_types:
        params["סוגי_רכב"] = vehicle_types

    brand = _first_field_value(occurrences, "יצרן")
    if brand:
        params["יצרן"] = brand
    model_name = _first_field_value(occurrences, "דגם")
    if model_name:
        params["דגם"] = model_name
    trim = _first_field_value(occurrences, "תת_דגם")
    if trim:
        params["תת_דגם"] = trim

    for field_name in ("צבע", "תיבת_הילוכים", "סוג_דלק", "בעלות"):
        value = _first_field_value(occurrences, field_name)
        if value:
            params[field_name] = value

    if _contains_keyword(canonical_query, "טעינה_מהירה"):
        params["טעינה_מהירה"] = True

    working_text, year = _extract_year(working_text)
    if year is not None:
        params["שנה"] = year

    working_text, hand = _extract_and_consume(working_text, r"(?<!\S)יד\s+(\d+)")
    if hand is not None:
        params["יד"] = hand

    working_text, km = _extract_and_consume(working_text, r"(\d+)\s*(?:ק\"מ|ק״מ|קילומטר|קילומטרים)")
    if km is not None:
        params["ק״מ"] = km

    working_text, horsepower = _extract_and_consume(working_text, r"(\d+)\s*(?:כ\"ס|כ״ס)")
    if horsepower is not None:
        params["הספק_כ״ס"] = horsepower

    working_text, engine_cc = _extract_and_consume(working_text, r"(\d+)\s*(?:סמ\"ק|סמ״ק|סיסי)")
    if engine_cc is not None:
        params["נפח_מנוע_סמ״ק"] = engine_cc

    price = _extract_price(working_text)
    if price is not None:
        params["מחיר"] = price

    return _validate(taxonomy_repository.params_models[Vertical.VEHICLES], params)


def extract_used_goods_params(canonical_query: str, occurrences: list[TermOccurrence]) -> BaseModel:
    params: dict[str, Any] = {}
    working_text = canonical_query

    sector = _first_field_value(occurrences, "סקטור")
    if sector:
        params["סקטور"] = sector
    subcategory = _first_field_value(occurrences, "תת_קטגוריה")
    if subcategory:
        params["תת_קטגוריה"] = subcategory

    for field_name in (
        "מותג", "מצב", "צבע", "אזור", "עיר", "מעבד", "טכנולוגיה",
        "רזולוציה", "סוגי_רהיט", "חומר", "סוג",
    ):
        value = _first_field_value(occurrences, field_name)
        if value:
            params[field_name] = value

    storage_match = re.search(r"(\d+)\s*(?:GB|ג'יגה|גיגה)", working_text)
    if storage_match:
        params["נפח_אחסון"] = f"{storage_match.group(1)}GB"
        working_text = working_text[: storage_match.start()] + " " * len(storage_match.group(0)) + working_text[storage_match.end() :]

    working_text, screen_size = _extract_and_consume(working_text, r"(\d+)\s*(?:אינץ|אינצ׳)")
    if screen_size is not None:
        params["גודל_אינצ׳"] = screen_size

    working_text, year = _extract_year(working_text)
    if year is not None:
        params["שנת_ייצור"] = year

    price = _extract_price(working_text)
    if price is not None:
        params["מחיר"] = price

    return _validate(taxonomy_repository.params_models[Vertical.USED_GOODS], params)


EXTRACTORS = {
    Vertical.REAL_ESTATE: extract_real_estate_params,
    Vertical.VEHICLES: extract_vehicle_params,
    Vertical.USED_GOODS: extract_used_goods_params,
}


def extract_params(vertical: Vertical, canonical_query: str, occurrences: list[TermOccurrence]) -> BaseModel:
    return EXTRACTORS[vertical](canonical_query, occurrences)
