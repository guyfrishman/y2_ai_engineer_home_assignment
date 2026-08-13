from fastapi import APIRouter, HTTPException, Response, status

from app.logger import log_activity
from app.schema.requests import ParseRequest
from app.schema.responses import ParseResponse
from app.services.parse_service import parse_query
from app.services.sanitizer_service import QueryRejectedError

router = APIRouter()


@router.post("/parse", summary="Parse a Hebrew free-text search query", response_model=ParseResponse)
@log_activity
async def parse(request: ParseRequest, response: Response):
    # parse_query is async end-to-end on the LLM-fallback branch (the
    # service's only real network I/O) via OpenAIRepository's AsyncOpenAI
    # client, so a slow model call yields the event loop instead of
    # blocking every other concurrent request. The cache/rules branches are
    # cheap, CPU-bound sync code that runs inline — no thread pool needed
    # for work that fast.
    try:
        result = await parse_query(request.q)
    except QueryRejectedError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    # Not part of the response body (the brief's schema is fixed to
    # category/params/confidence/notes) — an observability-only header so
    # scripts/loadtest.py can bucket latency by which tier actually resolved
    # the request.
    response.headers["X-Parse-Path"] = result.path
    return result.response
