"""Structured error envelope shared by every tool (SOP section 4.2).

Tools never raise exceptions to the caller: upstream failures are caught
and mapped to one of a fixed, small set of error codes, then returned as
a normal (string) tool result so the Agent can branch on `code` and
`retryable` instead of having the call chain interrupted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# Fixed error code vocabulary -- do not invent new codes (SOP section 4.2).
NOT_CONFIGURED = "not_configured"
UNAUTHORIZED = "unauthorized"
NOT_FOUND = "not_found"
INVALID_ARGUMENT = "invalid_argument"
RATE_LIMITED = "rate_limited"
UPSTREAM_ERROR = "upstream_error"

_RETRYABLE = {RATE_LIMITED, UPSTREAM_ERROR}


class UpstreamError(Exception):
    """Raised by api_client.py for any non-2xx Datadog response.

    Carries the HTTP status code so the tool layer can map it to the
    fixed error-code vocabulary without re-parsing the exception message.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class ToolError:
    code: str
    message: str
    retryable: bool

    def to_json(self) -> str:
        return json.dumps(
            {
                "error": {
                    "code": self.code,
                    "message": self.message,
                    "retryable": self.retryable,
                }
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )


def from_status_code(status_code: int, message: str) -> ToolError:
    """Map an upstream HTTP status code to the fixed error-code vocabulary."""
    if status_code in (401, 403):
        code = UNAUTHORIZED
    elif status_code == 404:
        code = NOT_FOUND
    elif status_code in (400, 422):
        code = INVALID_ARGUMENT
    elif status_code == 429:
        code = RATE_LIMITED
    elif status_code >= 500:
        code = UPSTREAM_ERROR
    else:
        code = UPSTREAM_ERROR
    return ToolError(code=code, message=message, retryable=code in _RETRYABLE)


def not_configured(message: str) -> str:
    return ToolError(NOT_CONFIGURED, message, retryable=False).to_json()


def invalid_argument(message: str) -> str:
    return ToolError(INVALID_ARGUMENT, message, retryable=False).to_json()


def from_upstream_error(exc: UpstreamError) -> str:
    return from_status_code(exc.status_code, exc.message).to_json()
