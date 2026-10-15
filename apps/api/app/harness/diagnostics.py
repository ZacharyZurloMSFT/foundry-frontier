"""Allow-listed failure metadata only; never format exception text or tracebacks."""
from __future__ import annotations

import hashlib
import logging


def log_failure(logger: logging.Logger, component: str, stage: str, error: BaseException | object,
                correlation: str = "") -> None:
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    data = getattr(error, "data", None)
    if status is None and isinstance(data, dict):
        status = data.get("statusCode", data.get("status_code"))
    status = status if type(status) is int and 100 <= status <= 599 else None
    code = getattr(error, "code", None)
    code = code if type(code) is int and -32768 <= code <= 32767 else None
    correlation_hash = hashlib.sha256(correlation.encode()).hexdigest()[:16] if correlation else "none"
    fields = {"component": component, "stage": stage, "error_class": type(error).__name__,
              "http_status": status, "rpc_code": code, "correlation_hash": correlation_hash}
    logger.error(
        "safe_failure component=%s stage=%s error_class=%s http_status=%s rpc_code=%s correlation_hash=%s",
        component, stage, fields["error_class"], status, code, correlation_hash,
        extra=fields, exc_info=None, stack_info=False,
    )
