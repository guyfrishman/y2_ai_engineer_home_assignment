from typing import Any

from pydantic import BaseModel

from app.schema.taxonomy_models import Vertical


class ParseResponse(BaseModel):
    category: Vertical
    params: dict[str, Any]
    confidence: float
    notes: list[str] = []


class HealthResponse(BaseModel):
    status: str
    taxonomy_version: str
