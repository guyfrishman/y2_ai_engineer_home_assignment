"""Structured JSON event logging — one function, ``log_event``. See
docs/conventions/logging.md for the shape and why the previous
@log_activity-decorator-on-every-function design was replaced with this.
"""

import json
import logging
from contextvars import ContextVar

from app.config import settings

# Request-scoped trace id. Set once per request by main.py's middleware,
# not lazily by whichever function happens to run first — see main.py.
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="n/a")

logger = logging.getLogger("app_logger")
logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
logger.propagate = False

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

for handler in logger.handlers:
    handler.setLevel(logger.level)


def _truncate(value):
    """Recursively cap long strings/bytes so one oversized value (a query,
    a stack trace) can't dominate a log line."""
    if isinstance(value, str):
        if len(value) > settings.max_str_log_length:
            return value[: settings.max_str_log_length] + f"... [{len(value)} chars]"
        return value
    if isinstance(value, bytes):
        return f"<bytes {len(value)} bytes>" if len(value) > settings.max_str_log_length else value.decode(
            "utf-8", errors="replace"
        )
    if isinstance(value, dict):
        return {key: _truncate(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_truncate(item) for item in value)
    return value


def log_event(**fields) -> None:
    """Emit one structured JSON log line, tagged with the current trace_id.

    This is the one logging entry point in the app — decision-point events
    (``cache_lookup``, ``parse_decision``, ``llm_call_outcome``, every
    ``security_*`` event), the once-per-request ``request_completed``
    summary, and the exception-boundary error event all go through this.
    """
    event = {"trace_id": trace_id_var.get(), **{key: _truncate(value) for key, value in fields.items()}}
    logger.info(json.dumps(event, default=str, ensure_ascii=False))
