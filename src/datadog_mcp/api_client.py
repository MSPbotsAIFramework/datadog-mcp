"""Async wrapper around the Datadog HTTP API.

One instance is built per request from the credentials in that request's
ContextVar (see server.py) -- never from environment variables or a
module-level singleton (SOP section 3.1 / 3.3).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from datadog_mcp.errors import INVALID_ARGUMENT, ToolError, UpstreamError

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

    def __init__(self, api_key: str, application_key: str, site: str | None = None) -> None:
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
        """Issue one request with bounded retry on 429/5xx, honoring Retry-After."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await self._client.request(method, path, params=params, json=json_body)
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt >= _MAX_RETRIES:
                    raise UpstreamError(599, f"Timeout calling Datadog API: {exc}") from exc
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
                continue
            except httpx.HTTPError as exc:
                raise UpstreamError(599, f"Network error calling Datadog API: {exc}") from exc

            if response.status_code < 400:
                if not response.content:
                    return {}
                return response.json()

            if response.status_code in _RETRY_STATUS_CODES and attempt < _MAX_RETRIES:
                delay = _retry_delay(response, attempt)
                await asyncio.sleep(delay)
                continue

            raise UpstreamError(response.status_code, _safe_error_message(response))

        # Unreachable, but keeps type-checkers happy.
        if last_exc is not None:
            raise UpstreamError(599, str(last_exc))
        raise UpstreamError(599, "Datadog API call failed with no response")


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
