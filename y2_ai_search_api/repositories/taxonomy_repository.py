"""Loads and indexes the vendored taxonomy once at import time.

The taxonomy is read-only, versioned configuration data, not a datastore —
there is no write path, so this repository only ever builds lookups over a
JSON file loaded once at process start.
"""

import hashlib
import json
from pathlib import Path
from typing import NamedTuple

from schema.taxonomy_models import (
    Vertical,
    build_real_estate_params_model,
    build_used_goods_params_model,
    build_vehicle_params_model,
)

TAXONOMY_FILE_PATH = Path(__file__).resolve().parent.parent / "data" / "taxonomy.json"

# "מותגים" (plural) is how used-goods subcategories name their brand-enum
# source list, but the actual output field is "מותג" (singular) — see the
# matching exclusion in schema.taxonomy_models.build_used_goods_params_model.
USED_GOODS_FIELD_NAME_ALIASES: dict[str, str] = {"מותגים": "מותג"}


class TaxonomyTermMatch(NamedTuple):
    """One known taxonomy term and the vertical/field it belongs to — the unit
    the classifier scores coverage with and the extractor fills fields from."""

    vertical: Vertical
    field_name: str


class TaxonomyRepository:
    def __init__(self, taxonomy_file_path: Path = TAXONOMY_FILE_PATH) -> None:
        raw_bytes = taxonomy_file_path.read_bytes()
        # A content hash, not the taxonomy's own "גרסה" field, so any edit to
        # the file — including one that forgets to bump גרסה — still
        # invalidates the cache (see CacheRepository's key scheme).
        self.taxonomy_version = hashlib.sha256(raw_bytes).hexdigest()[:16]
        self.raw = json.loads(raw_bytes)

        categories = self.raw["קטגוריות"]
        self.real_estate = categories["נדל״ן"]
        self.vehicles = categories["רכב"]
        self.used_goods = categories["יד_שנייה"]

        self.params_models: dict[Vertical, type] = {
            Vertical.REAL_ESTATE: build_real_estate_params_model(self.real_estate),
            Vertical.VEHICLES: build_vehicle_params_model(self.vehicles),
            Vertical.USED_GOODS: build_used_goods_params_model(self.used_goods),
        }

        self.typo_maps: dict[Vertical, dict[str, str]] = {
            Vertical.REAL_ESTATE: self.real_estate.get("מיפוי_מילות_שגיאה", {}),
            Vertical.VEHICLES: self.vehicles.get("מיפוי_מילות_שגיאה", {}),
            Vertical.USED_GOODS: self.used_goods.get("מיפוי_מילות_שגיאה", {}),
        }

        self.brand_models: dict[str, list[str]] = {
            brand: brand_data.get("דגמים", [])
            for brand, brand_data in self.vehicles.get("יצרנים", {}).items()
        }

        self.term_index: dict[str, list[TaxonomyTermMatch]] = self._build_term_index()
        # Longest terms first, so multi-word matches (e.g. "תל אביב-יפו") are
        # tried before any single-word term inside them.
        self.known_terms_by_length: list[str] = sorted(
            self.term_index.keys(), key=len, reverse=True
        )

    def _build_term_index(self) -> dict[str, list[TaxonomyTermMatch]]:
        index: dict[str, list[TaxonomyTermMatch]] = {}

        def add_term(term: str, vertical: Vertical, field_name: str) -> None:
            if not term:
                return
            # The same value (e.g. "כמו חדש") legitimately recurs across
            # several used-goods subcategories that share a field name —
            # dedupe so it doesn't get over-weighted in coverage scoring.
            existing = index.setdefault(term, [])
            match = TaxonomyTermMatch(vertical, field_name)
            if match not in existing:
                existing.append(match)

        self._index_real_estate_terms(add_term)
        self._index_vehicle_terms(add_term)
        self._index_used_goods_terms(add_term)
        return index

    def _index_real_estate_terms(self, add_term) -> None:
        for property_type in self.real_estate.get("סוגי_נכס", []):
            add_term(property_type, Vertical.REAL_ESTATE, "סוגי_נכס")
        for transaction_type in self.real_estate.get("מצבי_עסקה", []):
            add_term(transaction_type, Vertical.REAL_ESTATE, "מצבי_עסקה")
        general_attributes = self.real_estate.get("מאפיינים_כלליים", {})
        for city in general_attributes.get("עיר", {}).get("דוגמאות", []):
            add_term(city, Vertical.REAL_ESTATE, "עיר")
        for field_name, attribute_definition in general_attributes.items():
            if isinstance(attribute_definition, list):
                for value in attribute_definition:
                    add_term(value, Vertical.REAL_ESTATE, field_name)

    def _index_vehicle_terms(self, add_term) -> None:
        for vehicle_type in self.vehicles.get("סוגי_רכב", []):
            add_term(vehicle_type, Vertical.VEHICLES, "סוגי_רכב")
        for brand, brand_data in self.vehicles.get("יצרנים", {}).items():
            add_term(brand, Vertical.VEHICLES, "יצרן")
            for model_name in brand_data.get("דגמים", []):
                # A handful of models (e.g. Mazda "3") are bare digits — far
                # too ambiguous against room counts, years, etc. to serve as
                # a global classification signal. Extraction still resolves
                # them via brand_models once a brand is already matched.
                if model_name.isdigit():
                    continue
                add_term(model_name, Vertical.VEHICLES, "דגם")
            for trim_list in brand_data.get("תתי_דגמים", {}).values():
                for trim in trim_list:
                    add_term(trim, Vertical.VEHICLES, "תת_דגם")
        general_attributes = self.vehicles.get("מאפיינים_כלליים", {})
        for field_name, attribute_definition in general_attributes.items():
            if isinstance(attribute_definition, list):
                for value in attribute_definition:
                    add_term(value, Vertical.VEHICLES, field_name)

    def _index_used_goods_terms(self, add_term) -> None:
        for sector, sector_data in self.used_goods.get("סקטורים", {}).items():
            add_term(sector, Vertical.USED_GOODS, "סקטור")
            for subcategory, subcategory_data in sector_data.get("תתי_קטגוריות", {}).items():
                add_term(subcategory, Vertical.USED_GOODS, "תת_קטגוריה")
                for field_name, value in subcategory_data.items():
                    field_name = USED_GOODS_FIELD_NAME_ALIASES.get(field_name, field_name)
                    if isinstance(value, list):
                        for item in value:
                            add_term(item, Vertical.USED_GOODS, field_name)
        general_attributes = self.used_goods.get("מאפיינים_כלליים", {})
        for field_name, attribute_definition in general_attributes.items():
            if isinstance(attribute_definition, list):
                for value in attribute_definition:
                    add_term(value, Vertical.USED_GOODS, field_name)


taxonomy_repository = TaxonomyRepository()
