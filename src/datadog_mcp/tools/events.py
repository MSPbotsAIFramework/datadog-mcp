"""Events: search with a query and time range.

Natural companion to monitors/logs for "what happened and why" investigative
flows (Decisions made by DEV01, docs/tickets/PRD-21259.md).
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
    async def datadog_search_events(
        query: str,
        time_from: str,
        time_to: str,
        limit: int = DEFAULT_LIMIT,
        cursor: str = "",
    ) -> str:
        """Search events matching a query over a time range.

        Args: query - Datadog event search syntax. time_from/time_to - RFC3339 timestamp or
        relative expression (e.g. "now-1h", "now"). limit - max events to return (default 50,
        cap 200). cursor - pagination cursor from a previous call's response, empty for the first page.
        An empty result set is normal (no matching events), not an error.
        """
        capped_limit = min(limit, MAX_LIMIT)
        try:
            client: DatadogClient = client_factory()
        except InvalidSiteError as exc:
            return ToolError(code="invalid_argument", message=str(exc), retryable=False).to_json()

        body: dict[str, Any] = {
            "filter": {"query": query, "from": time_from, "to": time_to},
            "page": {"limit": capped_limit},
        }
        if cursor:
            body["page"]["cursor"] = cursor

        async with client:
            try:
                response = await client.request("POST", "/api/v2/events/search", json_body=body)
            except UpstreamError as exc:
                return from_upstream_error(exc)

        events = response.get("data", [])
        trimmed = [
            {
                "id": item.get("id"),
                "timestamp": item.get("attributes", {}).get("timestamp"),
                "title": item.get("attributes", {}).get("attributes", {}).get("title"),
                "message": item.get("attributes", {}).get("attributes", {}).get("message"),
                "tags": item.get("attributes", {}).get("tags", []),
            }
            for item in events
        ]
        next_cursor = response.get("meta", {}).get("page", {}).get("after")
        result: dict[str, Any] = {"events": trimmed}
        if next_cursor:
            result["cursor"] = next_cursor
        return _dumps(result)
