from fastapi import Depends, FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import settings
from app.routers.api import api_router
from app.routers.ping import router as ping_router
from app.security import verify_api_key

app = FastAPI(title=settings.project_name, version=settings.version)

# /health is open (liveness/readiness probe). /parse sits behind the API
# key (a no-op when API_ACCESS_KEY is unset — see app/security.py).
app.include_router(ping_router)
app.include_router(api_router, dependencies=[Depends(verify_api_key)])

# Adds GET /metrics, open, alongside HTTP-level request/status/latency
# instrumentation. Custom pipeline metrics live in app/metrics.py.
Instrumentator().instrument(app).expose(app)
