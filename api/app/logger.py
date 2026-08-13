import json
import logging
import functools
import inspect
import os
import uuid
from contextvars import ContextVar

from dotenv import load_dotenv

load_dotenv()

# Request-scoped identifiers. Every log line carries these so a single
# request can be traced across services, even through async boundaries.
session_id_var: ContextVar[str] = ContextVar("session_id", default="n/a")
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="n/a")

logger = logging.getLogger("app_logger")
default_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logger.setLevel(getattr(logging, default_log_level, logging.INFO))
logger.propagate = False

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

for handler in logger.handlers:
    handler.setLevel(logger.level)

MAX_STR_LOG_LENGTH = int(os.getenv("MAX_STR_LOG_LENGTH", "200"))


def truncate_value(value, max_len=MAX_STR_LOG_LENGTH):
    """Recursively truncate long strings/bytes in dicts, lists, tuples, and plain values."""
    if isinstance(value, str):
        if len(value) > max_len:
            return value[:max_len] + f"... [{len(value)} chars]"
        return value
    if isinstance(value, bytes):
        if len(value) > max_len:
            return f"<bytes {len(value)} bytes>"
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {k: truncate_value(v, max_len) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(truncate_value(v, max_len) for v in value)
    return value


def log_activity(func):
    """Decorator that emits structured STARTING / FINISHED / ERROR logs around
    a handler or service method, tagged with the current session and trace ids.

    Works on both sync and async callables. If no session/trace id is present
    in the call, fresh ones are generated and cleared on exit.
    """
    process_name = func.__name__

    def initialize_ids(args, kwargs):
        created_trace = False
        created_session = False

        request_obj = kwargs.get("request")
        if request_obj is None:
            for arg in args:
                if hasattr(arg, "session_id") or hasattr(arg, "trace_id"):
                    request_obj = arg
                    break

        if trace_id_var.get() == "n/a":
            trace_id = kwargs.get("trace_id")
            if trace_id:
                trace_id_var.set(trace_id)
            elif request_obj and getattr(request_obj, "trace_id", None):
                trace_id_var.set(request_obj.trace_id)
            else:
                trace_id_var.set(str(uuid.uuid4()))
            created_trace = True

        if session_id_var.get() == "n/a":
            session_id = kwargs.get("session_id")
            if session_id:
                session_id_var.set(session_id)
            elif request_obj and getattr(request_obj, "session_id", None):
                session_id_var.set(request_obj.session_id)
            else:
                session_id_var.set(str(uuid.uuid4()))
            created_session = True

        return created_trace, created_session

    def clear_ids(created_trace, created_session):
        if created_trace:
            trace_id_var.set("n/a")
        if created_session:
            session_id_var.set("n/a")

    def emit_log(status, inputs=None, outputs=None):
        log_event = {
            "status": status,
            "process": process_name,
            "session_id": session_id_var.get(),
            "trace_id": trace_id_var.get(),
        }

        if status == "ERROR":
            if outputs is not None:
                log_event["error"] = truncate_value(outputs)
            if inputs is not None:
                log_event["input"] = truncate_value(inputs)
            logger.error(json.dumps(log_event, default=str, ensure_ascii=False))

        elif logger.isEnabledFor(logging.DEBUG):
            if inputs is not None:
                log_event["input"] = truncate_value(inputs)
            if outputs is not None:
                log_event["output"] = truncate_value(outputs)
            logger.debug(json.dumps(log_event, default=str, ensure_ascii=False))

        else:
            # INFO: thin log — status, process, and ids only
            logger.info(json.dumps(log_event, default=str, ensure_ascii=False))

    def extract_inputs(args, kwargs):
        log_args = args[1:] if args and hasattr(args[0], "__dict__") else args
        exclude_keys = {"request", "response", "background_tasks"}
        clean_kwargs = {k: v for k, v in kwargs.items() if k not in exclude_keys}
        return {"args": log_args, "kwargs": clean_kwargs}

    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            created_trace, created_session = initialize_ids(args, kwargs)
            extracted_inputs = extract_inputs(args, kwargs)
            emit_log("STARTING", inputs=extracted_inputs)
            try:
                result = await func(*args, **kwargs)
                emit_log("FINISHED", outputs=result)
                return result
            except Exception as e:
                emit_log("ERROR", inputs=extracted_inputs, outputs={"error": str(e)})
                raise e
            finally:
                clear_ids(created_trace, created_session)

        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        created_trace, created_session = initialize_ids(args, kwargs)
        extracted_inputs = extract_inputs(args, kwargs)
        emit_log("STARTING", inputs=extracted_inputs)
        try:
            result = func(*args, **kwargs)
            emit_log("FINISHED", outputs=result)
            return result
        except Exception as e:
            emit_log("ERROR", inputs=extracted_inputs, outputs={"error": str(e)})
            raise e
        finally:
            clear_ids(created_trace, created_session)

    return sync_wrapper


def log_metric(**kwargs):
    """Emit a one-off METRIC log line (token counts, fallbacks, timings, security events, ...)."""
    log_event = {
        "status": "METRIC",
        "session_id": session_id_var.get(),
        "trace_id": trace_id_var.get(),
        **kwargs,
    }
    logger.info(json.dumps(log_event, default=str, ensure_ascii=False))
