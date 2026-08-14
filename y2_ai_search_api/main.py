import uuid

from fastapi import Depends, FastAPI, Request
from prometheus_fastapi_instrumentator import Instrumentator

from config import settings
from logger import log_event, trace_id_var
from routers.api import api_router
from routers.ping import router as ping_router
from security import verify_api_key

app = FastAPI(title=settings.project_name, version=settings.version)


@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    """Sets trace_id once per request, at the actual entry point — not
    lazily by whichever function happened to run first, the previous
    @log_activity design. Also the one place an unhandled exception gets
    logged: a single event at this true boundary, replacing
    @log_activity's per-function ERROR lines (one call could previously
    produce several, one per decorated function on the stack).
    """
    token = trace_id_var.set(str(uuid.uuid4()))
    try:
        return await call_next(request)
    except Exception as error:
        log_event(event="request_error", exception_type=type(error).__name__)
        raise
    finally:
        trace_id_var.reset(token)


# /health is open (liveness/readiness probe). /parse sits behind the API
# key (a no-op when API_ACCESS_KEY is unset — see security.py).
app.include_router(ping_router)
app.include_router(api_router, dependencies=[Depends(verify_api_key)])

# Adds GET /metrics, open, alongside HTTP-level request/status/latency
# instrumentation. Custom pipeline metrics live in metrics.py.
Instrumentator().instrument(app).expose(app)
