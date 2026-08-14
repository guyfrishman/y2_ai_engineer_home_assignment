from fastapi import Security, HTTPException, status
from fastapi.security import APIKeyHeader

from config import get_api_access_key

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(_api_key_header)) -> None:
    """Dependency that guards the API with an X-API-Key header.

    Local mode: if ``API_ACCESS_KEY`` is unset (empty), auth is a no-op and all
    requests are allowed — convenient for local development and grading.

    Protected mode: if ``API_ACCESS_KEY`` is set, requests must send a matching
    ``X-API-Key`` header or they are rejected with HTTP 403.
    """
    expected_key = get_api_access_key()
    if not expected_key:
        return
    if not api_key or api_key != expected_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing API key",
        )
