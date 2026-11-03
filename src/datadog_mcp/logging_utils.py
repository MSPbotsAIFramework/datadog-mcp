"""Structured, request-scoped observability (SOP section 6, PRD-21259 review round 1).

One JSON log line per upstream Datadog API call: tool name, request id, the
Datadog API path and status code, latency, and the mapped error `code` on
failure. Never logs credential header values or response bodies -- api_client.py
only ever passes this module a path, a status code, and an already-classified
error code, never raw request/response content.
"""

from __future__ import annotations

import contextvars
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger("datadog_mcp")

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "datadog_mcp_request_id", default="-"
)


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def set_request_id(request_id: str) -> contextvars.Token[str]:
    return _request_id_var.set(request_id)


def reset_request_id(token: contextvars.Token[str]) -> None:
    _request_id_var.reset(token)


def current_request_id() -> str:
    return _request_id_var.get()


class _JsonFormatter(logging.Formatter):
    """One JSON object per line; only fields passed via `extra=` are emitted
    beyond the fixed ts/level/msg triplet -- callers control exactly what
    goes out, there is no risk of an unrelated field leaking in."""

    _RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED:
                payload[key] = value
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotent -- safe to call every time build_app() runs (tests call it
    repeatedly in one process)."""
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def log_upstream_call(
    *,
    tool: str,
    method: str,
    path: str,
    status_code: int | None,
    latency_ms: float,
    error_code: str | None = None,
) -> None:
    """Log one upstream Datadog API call.

    Only ever pass a path (not query params/body) and an already-classified
    error `code` (not the raw exception message, which may echo upstream
    response content) -- never credential header values or response bodies.
    """
    logger.info(
        "datadog_api_call",
        extra={
            "request_id": current_request_id(),
            "tool": tool,
            "method": method,
            "path": path,
            "status_code": status_code,
            "latency_ms": round(latency_ms, 1),
            "error_code": error_code,
        },
    )
