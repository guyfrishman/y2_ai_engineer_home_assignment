from fastapi import APIRouter, HTTPException, Response, status

from schema.requests import ParseRequest
from schema.responses import ParseResponse
from services.parse_service import parse_query
from services.sanitizer_service import QueryRejectedError

router = APIRouter()


@router.post("/parse", summary="Parse a Hebrew free-text search query", response_model=ParseResponse)
async def parse(request: ParseRequest, response: Response):
    try:
        result = await parse_query(request.q)
    except QueryRejectedError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    response.headers["X-Parse-Path"] = result.path
    return result.response
