"""Pydantic models for the ``params`` object of a parsed search query.

The per-vertical models are built dynamically from the vendored taxonomy
(see ``repositories.taxonomy_repository``) rather than hand-written,
so ``data/taxonomy.json`` stays the single source of truth for which
fields exist. Every built model has ``extra="forbid"`` — a field name the
taxonomy doesn't define is rejected, never silently accepted or invented.
"""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, create_model, model_validator


class Vertical(str, Enum):
    """The three Yad2 marketplace verticals, keyed by their taxonomy name."""

    REAL_ESTATE = "נדל״ן"
    VEHICLES = "רכב"
    USED_GOODS = "יד_שנייה"


# ASCII slugs for contexts where Hebrew text is awkward (Prometheus label
# values, PromQL, Grafana legends) — the API response itself always uses
# the Hebrew taxonomy strings from Vertical.
VERTICAL_METRIC_LABELS: dict[Vertical, str] = {
    Vertical.REAL_ESTATE: "real_estate",
    Vertical.VEHICLES: "vehicles",
    Vertical.USED_GOODS: "used_goods",
}


class RangeValue(BaseModel):
    """A numeric range, e.g. {"min": 0, "max": 1000000} for a price ceiling."""

    model_config = ConfigDict(extra="forbid")

    min: float | int | None = None
    max: float | int | None = None

    @model_validator(mode="after")
    def require_at_least_one_bound(self) -> "RangeValue":
        if self.min is None and self.max is None:
            raise ValueError("a range must set at least one of min/max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("range min must not exceed range max")
        return self


NumberOrRange = float | int | RangeValue


class TaxonomyParamsBase(BaseModel):
    """Shared config for every generated per-vertical params model."""

    model_config = ConfigDict(extra="forbid")


def _pydantic_type_for_taxonomy_attribute(attribute_definition: Any) -> type:
    """Map one taxonomy attribute definition to the pydantic type used for it.

    A plain list of strings is an enum of allowed values — modeled as
    ``Literal[...]``, not a bare ``str``, so an out-of-taxonomy value is a
    validation error rather than silently accepted data. This is what makes
    "strict JSON Schema validation" true for both the rule path (which only
    ever assigns values it matched from the taxonomy anyway) and the LLM
    fallback path (where a hallucinated value is the real risk). A
    {"טיפוס": ...} dict names a scalar type; numeric attributes accept
    either a bare number or a {min,max} range, since a query can name an
    exact value ("3 חדרים") or a bound ("עד מיליון").
    """
    if isinstance(attribute_definition, list):
        return Literal[tuple(attribute_definition)] | None
    if isinstance(attribute_definition, dict):
        declared_type = attribute_definition.get("טיפוס")
        if declared_type == "מספר":
            return NumberOrRange | None
        if declared_type == "בוליאני":
            return bool | None
        return str | None
    return str | None


def _fields_from_general_attributes(general_attributes: dict[str, Any]) -> dict[str, tuple[type, None]]:
    """Turn a taxonomy מאפיינים_כלליים block into pydantic field definitions.

    First occurrence of a field name wins on type — the vendored taxonomy is
    internally consistent (e.g. every "מחיר" field is numeric), so this only
    matters when the same field name is merged in from multiple sources.
    """
    fields: dict[str, tuple[type, None]] = {}
    for field_name, attribute_definition in general_attributes.items():
        if field_name not in fields:
            fields[field_name] = (_pydantic_type_for_taxonomy_attribute(attribute_definition), None)
    return fields


def build_real_estate_params_model(real_estate_taxonomy: dict[str, Any]) -> type[BaseModel]:
    property_types = Literal[tuple(real_estate_taxonomy.get("סוגי_נכס", []))]
    transaction_types = Literal[tuple(real_estate_taxonomy.get("מצבי_עסקה", []))]
    fields: dict[str, tuple[type, None]] = {
        "מצבי_עסקה": (list[transaction_types] | None, None),
        "סוגי_נכס": (list[property_types] | None, None),
    }
    fields.update(_fields_from_general_attributes(real_estate_taxonomy.get("מאפיינים_כלליים", {})))
    return create_model("RealEstateParams", __base__=TaxonomyParamsBase, **fields)


def build_vehicle_params_model(vehicle_taxonomy: dict[str, Any]) -> type[BaseModel]:
    vehicle_types = Literal[tuple(vehicle_taxonomy.get("סוגי_רכב", []))]
    # "דגם"/"תת_דגם" stay free strings, not per-brand Literals — which
    # models are valid depends on which brand was matched, and enforcing
    # that cross-field relationship is a documented simplification left for
    # later, not a taxonomy-allowlist bypass (the field itself is still
    # closed; only its value isn't cross-checked against the chosen brand).
    brands = Literal[tuple(vehicle_taxonomy.get("יצרנים", {}).keys())]
    fields: dict[str, tuple[type, None]] = {
        "סוגי_רכב": (list[vehicle_types] | None, None),
        "יצרן": (brands | None, None),
        "דגם": (str | None, None),
        "תת_דגם": (str | None, None),
    }
    fields.update(_fields_from_general_attributes(vehicle_taxonomy.get("מאפיינים_כלליים", {})))
    return create_model("VehicleParams", __base__=TaxonomyParamsBase, **fields)


# "מותגים" (plural — "brands") is how subcategories name their brand-enum
# source list, but the actual output field is "מותג" (singular), already
# declared in מאפיינים_כלליים — a naming inconsistency in the taxonomy
# itself, not a real extra field, so it's excluded here rather than exposed.
_SUBCATEGORY_KEYS_EXCLUDED_AS_FIELDS = frozenset({"מותגים"})


def build_used_goods_params_model(used_goods_taxonomy: dict[str, Any]) -> type[BaseModel]:
    sectors_taxonomy = used_goods_taxonomy.get("סקטורים", {})
    sectors = Literal[tuple(sectors_taxonomy.keys())]
    subcategories = Literal[
        tuple(
            subcategory
            for sector_data in sectors_taxonomy.values()
            for subcategory in sector_data.get("תתי_קטגוריות", {})
        )
    ]
    # Both "סקטור" and "תת_קטגוריה" are individually valid Literal members on
    # their own, but a real, live LLM-fallback call was observed pairing
    # "תת_קטגוריה": "מחשבים_ניידים" (laptops) with "סקטור": "מוסיקה_וכלים"
    # (music) — a nonsensical combination that still passes per-field
    # validation. This cross-field check catches exactly that: if both are
    # present, the subcategory must actually belong to that sector.
    subcategory_to_sector: dict[str, str] = {
        subcategory: sector
        for sector, sector_data in sectors_taxonomy.items()
        for subcategory in sector_data.get("תתי_קטגוריות", {})
    }

    class UsedGoodsParamsBase(TaxonomyParamsBase):
        @model_validator(mode="after")
        def require_subcategory_to_belong_to_sector(self) -> "UsedGoodsParamsBase":
            sector = getattr(self, "סקטור", None)
            subcategory = getattr(self, "תת_קטגוריה", None)
            if sector is not None and subcategory is not None:
                if subcategory_to_sector.get(subcategory) != sector:
                    raise ValueError(f'תת_קטגוריה "{subcategory}" is not part of סקטור "{sector}"')
            return self

    fields: dict[str, tuple[type, None]] = {
        "סקטור": (sectors | None, None),
        "תת_קטגוריה": (subcategories | None, None),
    }
    # Subcategory-specific attributes (e.g. "נפח_אחסון" for phones, "חומר"
    # for furniture) are unioned across all sectors into one flat model —
    # which fields are *relevant* depends on the matched subcategory, but
    # which field *names* are allowed is still the full taxonomy allowlist.
    for sector_data in used_goods_taxonomy.get("סקטורים", {}).values():
        for subcategory_data in sector_data.get("תתי_קטגוריות", {}).values():
            for field_name, attribute_definition in subcategory_data.items():
                if field_name in _SUBCATEGORY_KEYS_EXCLUDED_AS_FIELDS:
                    continue
                if field_name not in fields:
                    fields[field_name] = (_pydantic_type_for_taxonomy_attribute(attribute_definition), None)
    fields.update(_fields_from_general_attributes(used_goods_taxonomy.get("מאפיינים_כלליים", {})))
    return create_model("UsedGoodsParams", __base__=UsedGoodsParamsBase, **fields)
