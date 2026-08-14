from fastapi import APIRouter
from routers import search

api_router = APIRouter()
api_router.include_router(search.router, tags=["Search"])
