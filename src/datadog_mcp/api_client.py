"""Async wrapper around the Datadog HTTP API.

One instance is built per request from the credentials in that request's
ContextVar (see server.py) -- never from environment variables or a
module-level singleton (SOP section 3.1 / 3.3).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from datadog_mcp.errors import INVALID_ARGUMENT, UPSTREAM_ERROR, ToolError, UpstreamError, from_status_code
from datadog_mcp.logging_utils import log_upstream_call

# Site -> API base URL (https://docs.datadoghq.com/getting_started/site/).
SITE_BASE_URLS: dict[str, str] = {
    "datadoghq.com": "https://api.datadoghq.com",
    "datadoghq.eu": "https://api.datadoghq.eu",
    "us3.datadoghq.com": "https://api.us3.datadoghq.com",
    "us5.datadoghq.com": "https://api.us5.datadoghq.com",
    "ap1.datadoghq.com": "https://api.ap1.datadoghq.com",
    "ddog-gov.com": "https://api.ddog-gov.com",
}

DEFAULT_SITE = "datadoghq.com"

_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 30.0
_MAX_RETRIES = 3
_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_BACKOFF_BASE_SECONDS = 0.5
# SOP section 5: a single tool call, including every retry and backoff sleep,
# must stay under 120s. _READ_TIMEOUT * (_MAX_RETRIES + 1) + backoff alone can
# exceed that (review round 1, finding 1), so request() enforces this ceiling
# directly against the clock instead of relying on those constants' arithmetic
# staying under 120s by coincidence.
_MAX_TOTAL_SECONDS = 100.0


class InvalidSiteError(Exception):
    """Raised when X-Datadog-Site is set to an unrecognized value."""

    def __init__(self, site: str) -> None:
        self.site = site
        super().__init__(
            f"Unrecognized Datadog site '{site}'. "
            f"Accepted values: {', '.join(sorted(SITE_BASE_URLS))}."
        )


def resolve_base_url(site: str | None) -> str:
    site = site or DEFAULT_SITE
    try:
        return SITE_BASE_URLS[site]
    except KeyError as exc:
        raise InvalidSiteError(site) from exc


class DatadogClient:
    """Per-request Datadog API client. Not shared across requests."""

    def __init__(
        self, api_key: str, application_key: str, site: str | None = None, *, tool_name: str = ""
    ) -> None:
        self._tool_name = tool_name
        self._base_url = resolve_base_url(site)
        self._headers = {
            "DD-API-KEY": api_key,
            "DD-APPLICATION-KEY": application_key,
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT, write=_READ_TIMEOUT, pool=_READ_TIMEOUT),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "DatadogClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue one request with bounded retry on 429/5xx, honoring Retry-After.

        Every attempt -- plus the backoff sleep before it, plus the sleep
        waiting on it -- is bounded against a single wall-clock deadline
        (_MAX_TOTAL_SECONDS), so the whole call can never reach the SOP's
        120s ceiling regardless of how many attempts or how slow upstream is.
        """
        start = time.monotonic()
        last_exc: Exception | None = None
        status_code: int | None = None
        error_code: str | None = None
        try:
            for attempt in range(_MAX_RETRIES + 1):
                remaining = _MAX_TOTAL_SECONDS - (time.monotonic() - start)
                if remaining <= 0:
                    error_code = UPSTREAM_ERROR
                    raise UpstreamError(599, "Datadog API call aborted: retry time budget exhausted")
                try:
                    response = await asyncio.wait_for(
                        self._client.request(method, path, params=params, json=json_body),
                        timeout=remaining,
                    )
                except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                    last_exc = exc
                    remaining = _MAX_TOTAL_SECONDS - (time.monotonic() - start)
                    if attempt >= _MAX_RETRIES or remaining <= 0:
                        error_code = UPSTREAM_ERROR
                        raise UpstreamError(599, f"Timeout calling Datadog API: {exc}") from exc
                    await asyncio.sleep(min(_BACKOFF_BASE_SECONDS * (2**attempt), remaining))
                    continue
                except httpx.HTTPError as exc:
                    error_code = UPSTREAM_ERROR
                    raise UpstreamError(599, f"Network error calling Datadog API: {exc}") from exc

                status_code = response.status_code
                if status_code < 400:
                    if not response.content:
                        return {}
                    return response.json()

                if status_code in _RETRY_STATUS_CODES and attempt < _MAX_RETRIES:
                    remaining = _MAX_TOTAL_SECONDS - (time.monotonic() - start)
                    if remaining <= 0:
                        error_code = from_status_code(status_code, "").code
                        raise UpstreamError(status_code, _safe_error_message(response))
                    await asyncio.sleep(min(_retry_delay(response, attempt), remaining))
                    continue

                error_code = from_status_code(status_code, "").code
                raise UpstreamError(status_code, _safe_error_message(response))

            # Unreachable, but keeps type-checkers happy.
            error_code = UPSTREAM_ERROR
            if last_exc is not None:
                raise UpstreamError(599, str(last_exc))
            raise UpstreamError(599, "Datadog API call failed with no response")
        finally:
            log_upstream_call(
                tool=self._tool_name,
                method=method,
                path=path,
                status_code=status_code,
                latency_ms=(time.monotonic() - start) * 1000,
                error_code=error_code,
            )


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return _BACKOFF_BASE_SECONDS * (2**attempt)


def _safe_error_message(response: httpx.Response) -> str:
    """Build an error message without leaking the raw response body (SOP section 6)."""
    try:
        body = response.json()
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return str(errors[0])[:300]
    except Exception:  # noqa: BLE001 - response body may not be JSON
        pass
    return f"Datadog API returned HTTP {response.status_code}"


def invalid_site_error(exc: InvalidSiteError) -> ToolError:
    return ToolError(code=INVALID_ARGUMENT, message=str(exc), retryable=False)
