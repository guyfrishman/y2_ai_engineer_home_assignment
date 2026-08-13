from fastapi import APIRouter

from app.logger import log_activity
from app.repositories.taxonomy_repository import taxonomy_repository
from app.schema.responses import HealthResponse

router = APIRouter()


@router.get("/health", summary="Liveness check", response_model=HealthResponse)
@log_activity
async def health():
    return HealthResponse(status="ok", taxonomy_version=taxonomy_repository.taxonomy_version)
