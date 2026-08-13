from fastapi import APIRouter

from app.repositories.taxonomy_repository import taxonomy_repository
from app.schema.responses import HealthResponse

router = APIRouter()


@router.get("/health", summary="Liveness check", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok", taxonomy_version=taxonomy_repository.taxonomy_version)
