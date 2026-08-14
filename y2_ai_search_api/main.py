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
    """Sets trace_id once per request, at the entry point"""
    token = trace_id_var.set(str(uuid.uuid4()))
    try:
        return await call_next(request)
    except Exception as error:
        log_event(event="request_error", exception_type=type(error).__name__)
        raise
    finally:
        trace_id_var.reset(token)

app.include_router(ping_router)
app.include_router(api_router, dependencies=[Depends(verify_api_key)])
Instrumentator().instrument(app).expose(app)
