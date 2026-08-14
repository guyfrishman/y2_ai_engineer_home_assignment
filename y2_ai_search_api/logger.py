"""Structured JSON event logging — one function, ``log_event``, called
explicitly at each meaningful pipeline step rather than via a decorator on
every function."""

import json
import logging
from contextvars import ContextVar

from config import settings

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


def log_event(*, level: str = "INFO", **fields) -> None:
    """Emit one structured JSON log line, tagged with the current trace_id.

    The one logging entry point in the app. ``level="DEBUG"`` for
    step-by-step pipeline internals (sanitize/normalize/classify/extract
    input+output, raw LLM request/response content); everything else
    (cache hit/miss, path taken, tier/classification outcomes, the final
    result) stays INFO. Actually filtered by LOG_LEVEL -- unlike a plain
    logger.info(...) call, a DEBUG-level event costs nothing (not even the
    json.dumps) when LOG_LEVEL=INFO.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    if not logger.isEnabledFor(numeric_level):
        return
    event = {"trace_id": trace_id_var.get(), **{key: _truncate(value) for key, value in fields.items()}}
    logger.log(numeric_level, json.dumps(event, default=str, ensure_ascii=False))
