"""Monitors: search, get detail, mute, unmute.

datadog_mute_monitor / datadog_unmute_monitor are the only write tools in
this service (Decisions made by DEV01, docs/tickets/PRD-21259.md) -- every
other tool here and in the sibling modules is read-only.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from datadog_mcp.api_client import DatadogClient, InvalidSiteError
from datadog_mcp.errors import NOT_FOUND, ToolError, UpstreamError, from_upstream_error

MAX_PER_PAGE = 100
DEFAULT_PER_PAGE = 30


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _summary(monitor: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": monitor.get("id"),
        "name": monitor.get("name"),
        "type": monitor.get("type"),
        "overall_state": monitor.get("overall_state"),
        "tags": monitor.get("tags", []),
        "query": monitor.get("query"),
    }


def register(mcp: FastMCP, client_factory) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def datadog_search_monitors(query: str = "", page: int = 0, per_page: int = DEFAULT_PER_PAGE) -> str:
        """List monitors matching an optional search query.

        Args: query - Datadog monitor search syntax (e.g. "status:alert tag:env:prod"), empty for all monitors.
        page - zero-based page number. per_page - results per page (capped at 100).
        """
        capped_per_page = min(per_page, MAX_PER_PAGE)
        try:
            client: DatadogClient = client_factory()
        except InvalidSiteError as exc:
            return ToolError(code="invalid_argument", message=str(exc), retryable=False).to_json()
        async with client:
            try:
                body = await client.request(
                    "GET",
                    "/api/v1/monitor/search",
                    params={"query": query, "page": page, "per_page": capped_per_page} if query else {"page": page, "per_page": capped_per_page},
                )
            except UpstreamError as exc:
                return from_upstream_error(exc)

        monitors = [_summary(m) for m in body.get("monitors", [])]
        metadata = body.get("metadata", {})
        page_count = metadata.get("page_count", page + 1)
        has_more = (page + 1) < page_count
        return _dumps({"monitors": monitors, "page": page, "per_page": capped_per_page, "has_more": has_more})

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def datadog_get_monitor(monitor_id: int) -> str:
        """Get full detail (definition, state, tags, options) for one monitor by id.

        Args: monitor_id - the Datadog monitor id.
        """
        try:
            client: DatadogClient = client_factory()
        except InvalidSiteError as exc:
            return ToolError(code="invalid_argument", message=str(exc), retryable=False).to_json()
        async with client:
            try:
                body = await client.request("GET", f"/api/v1/monitor/{monitor_id}")
            except UpstreamError as exc:
                if exc.status_code == 404:
                    return ToolError(NOT_FOUND, f"Monitor {monitor_id} does not exist.", retryable=False).to_json()
                return from_upstream_error(exc)
        return _dumps(body)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
    async def datadog_mute_monitor(monitor_id: int, scope: str = "", end: int | None = None) -> str:
        """Mute (silence) a monitor, optionally scoped to one tag and/or until a Unix timestamp.

        Reversible via datadog_unmute_monitor. Not idempotent: calling this again on an
        already-muted monitor updates (does not stack) its mute window.
        Args: monitor_id - the Datadog monitor id. scope - optional tag scope (e.g. "host:web-01"),
        empty mutes the whole monitor. end - optional Unix epoch seconds when the mute expires.
        """
        payload: dict[str, Any] = {}
        if scope:
            payload["scope"] = scope
        if end is not None:
            payload["end"] = end
        try:
            client: DatadogClient = client_factory()
        except InvalidSiteError as exc:
            return ToolError(code="invalid_argument", message=str(exc), retryable=False).to_json()
        async with client:
            try:
                body = await client.request("POST", f"/api/v1/monitor/{monitor_id}/mute", json_body=payload)
            except UpstreamError as exc:
                if exc.status_code == 404:
                    return ToolError(NOT_FOUND, f"Monitor {monitor_id} does not exist.", retryable=False).to_json()
                return from_upstream_error(exc)
        return _dumps(
            {
                "id": body.get("id", monitor_id),
                "muted": True,
                "options": body.get("options", {}),
            }
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    async def datadog_unmute_monitor(monitor_id: int, scope: str = "") -> str:
        """Clear a mute on a monitor (whole monitor, or just one scope).

        Idempotent: unmuting an already-active monitor is a no-op that still returns success.
        Args: monitor_id - the Datadog monitor id. scope - optional tag scope to unmute, empty unmutes the whole monitor.
        """
        payload: dict[str, Any] = {}
        if scope:
            payload["scope"] = scope
        try:
            client: DatadogClient = client_factory()
        except InvalidSiteError as exc:
            return ToolError(code="invalid_argument", message=str(exc), retryable=False).to_json()
        async with client:
            try:
                body = await client.request("POST", f"/api/v1/monitor/{monitor_id}/unmute", json_body=payload)
            except UpstreamError as exc:
                if exc.status_code == 404:
                    return ToolError(NOT_FOUND, f"Monitor {monitor_id} does not exist.", retryable=False).to_json()
                return from_upstream_error(exc)
        return _dumps(
            {
                "id": body.get("id", monitor_id),
                "muted": False,
                "options": body.get("options", {}),
            }
        )
