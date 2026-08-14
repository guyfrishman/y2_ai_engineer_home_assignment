from fastapi import APIRouter

from routers import search

# Composes the API surface from bare per-feature routers. Mounted with no
# prefix in main.py — the brief calls the endpoint as "POST /parse" verbatim
# and there's no versioned/UI consumer here to justify one.
api_router = APIRouter()
api_router.include_router(search.router, tags=["Search"])
