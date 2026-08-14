from typing import Any

from pydantic import BaseModel

from schema.taxonomy_models import Vertical


class ParseResponse(BaseModel):
    # None: a well-formed query that genuinely doesn't belong to any of the
    # three verticals (or reads as an instruction, not a search) -- an
    # honest null beats forcing a wrong category.
    category: Vertical | None
    params: dict[str, Any]
    confidence: float
    notes: list[str] = []


class HealthResponse(BaseModel):
    status: str
    taxonomy_version: str
