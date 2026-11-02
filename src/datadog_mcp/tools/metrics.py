"""Metrics: query a timeseries, list active metric names.

datadog_list_active_metrics truncates client-side with an optional prefix
filter because the upstream endpoint has no native page size (Decisions
made by DEV01, docs/tickets/PRD-21259.md).
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from datadog_mcp.api_client import DatadogClient, InvalidSiteError
from datadog_mcp.errors import ToolError, UpstreamError, from_upstream_error

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def register(mcp: FastMCP, client_factory) -> None:
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def datadog_query_metrics(query: str, time_from: int, time_to: int) -> str:
        """Query a metric timeseries (e.g. "avg:system.cpu.user{*}") over a time range.

        Args: query - a Datadog metric query string. time_from/time_to - Unix epoch seconds.
        """
        try:
            client: DatadogClient = client_factory()
        except InvalidSiteError as exc:
            return ToolError(code="invalid_argument", message=str(exc), retryable=False).to_json()

        async with client:
            try:
                body = await client.request(
                    "GET", "/api/v1/query", params={"query": query, "from": time_from, "to": time_to}
                )
            except UpstreamError as exc:
                return from_upstream_error(exc)

        series = [
            {
                "metric": s.get("metric"),
                "scope": s.get("scope"),
                "unit": s.get("unit"),
                "pointlist": s.get("pointlist", []),
            }
            for s in body.get("series", [])
        ]
        return _dumps({"series": series, "query": body.get("query", query)})

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def datadog_list_active_metrics(time_from: int, prefix: str = "", limit: int = DEFAULT_LIMIT) -> str:
        """List metric names reported active since a given time, optionally filtered by prefix.

        Truncated client-side (the upstream endpoint has no page size) -- check `has_more`
        before assuming the list is complete.
        Args: time_from - Unix epoch seconds. prefix - optional, only names starting with this
        string are returned. limit - max names to return (default 50, cap 200).
        """
        capped_limit = min(limit, MAX_LIMIT)
        try:
            client: DatadogClient = client_factory()
        except InvalidSiteError as exc:
            return ToolError(code="invalid_argument", message=str(exc), retryable=False).to_json()

        async with client:
            try:
                body = await client.request("GET", "/api/v1/metrics", params={"from": time_from})
            except UpstreamError as exc:
                return from_upstream_error(exc)

        names: list[str] = body.get("metrics", [])
        if prefix:
            names = [name for name in names if name.startswith(prefix)]

        has_more = len(names) > capped_limit
        return _dumps({"metrics": names[:capped_limit], "has_more": has_more})
