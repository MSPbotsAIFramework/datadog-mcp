"""tools/list snapshot, per-domain success mapping, and error-code mapping.

All Datadog HTTP calls are mocked -- these tests never touch the network
(SOP section 10).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from datadog_mcp.api_client import DatadogClient, resolve_base_url
from datadog_mcp.errors import UpstreamError
from datadog_mcp.server import RequestCredentials, _credentials, build_mcp

EXPECTED_TOOLS = {
    "datadog_search_monitors": set(),
    "datadog_get_monitor": {"monitor_id"},
    "datadog_mute_monitor": {"monitor_id"},
    "datadog_unmute_monitor": {"monitor_id"},
    "datadog_search_logs": {"query", "time_from", "time_to"},
    "datadog_query_metrics": {"query", "time_from", "time_to"},
    "datadog_list_active_metrics": {"time_from"},
    "datadog_search_events": {"query", "time_from", "time_to"},
}


@pytest.fixture
def mcp():
    return build_mcp()


@asynccontextmanager
async def _with_credentials(site: str | None = None):
    token = _credentials.set(RequestCredentials(api_key="test-key", application_key="test-app-key", site=site))
    try:
        yield
    finally:
        _credentials.reset(token)


async def _call(mcp, name: str, args: dict) -> dict:
    _, structured = await mcp.call_tool(name, args)
    return json.loads(structured["result"])


def _mock_request(return_value=None, side_effect=None):
    return patch.object(DatadogClient, "request", new=AsyncMock(return_value=return_value, side_effect=side_effect))


@pytest.mark.asyncio
async def test_tools_list_snapshot(mcp) -> None:
    tools = await mcp.list_tools()
    by_name = {t.name: t for t in tools}

    assert set(by_name) == set(EXPECTED_TOOLS)
    for name, required in EXPECTED_TOOLS.items():
        schema = by_name[name].inputSchema
        assert set(schema.get("required", [])) == required, name


@pytest.mark.asyncio
async def test_search_monitors_returns_paged_summaries(mcp) -> None:
    upstream = {
        "monitors": [
            {"id": 1, "name": "High CPU", "type": "metric alert", "overall_state": "Alert", "tags": ["env:prod"], "query": "q1"},
            {"id": 2, "name": "Low disk", "type": "metric alert", "overall_state": "OK", "tags": ["env:prod"], "query": "q2"},
        ],
        "metadata": {"page_count": 1},
    }
    async with _with_credentials():
        with _mock_request(return_value=upstream):
            result = await _call(mcp, "datadog_search_monitors", {})

    assert [m["id"] for m in result["monitors"]] == [1, 2]
    assert result["page"] == 0
    assert result["per_page"] == 30
    assert result["has_more"] is False


@pytest.mark.asyncio
async def test_search_monitors_caps_per_page_at_100(mcp) -> None:
    async with _with_credentials():
        mock = AsyncMock(return_value={"monitors": [], "metadata": {"page_count": 1}})
        with patch.object(DatadogClient, "request", new=mock):
            await _call(mcp, "datadog_search_monitors", {"per_page": 500})

    _, kwargs = mock.call_args
    assert kwargs["params"]["per_page"] == 100


@pytest.mark.asyncio
async def test_get_monitor_returns_full_detail(mcp) -> None:
    detail = {"id": 42, "name": "High CPU", "type": "metric alert", "options": {"thresholds": {"critical": 90}}}
    async with _with_credentials():
        with _mock_request(return_value=detail):
            result = await _call(mcp, "datadog_get_monitor", {"monitor_id": 42})

    assert result == detail


@pytest.mark.asyncio
async def test_get_monitor_maps_404_to_not_found(mcp) -> None:
    async with _with_credentials():
        with _mock_request(side_effect=UpstreamError(404, "not found")):
            result = await _call(mcp, "datadog_get_monitor", {"monitor_id": 999999999})

    assert result["error"]["code"] == "not_found"
    assert result["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_mute_monitor_confirms_muted_state(mcp) -> None:
    async with _with_credentials():
        with _mock_request(return_value={"id": 42, "options": {"scope": "host:web-01", "end": 1700000000}}):
            result = await _call(mcp, "datadog_mute_monitor", {"monitor_id": 42, "scope": "host:web-01", "end": 1700000000})

    assert result["muted"] is True
    assert result["options"]["scope"] == "host:web-01"
    assert result["options"]["end"] == 1700000000


@pytest.mark.asyncio
async def test_unmute_monitor_confirms_active_state(mcp) -> None:
    async with _with_credentials():
        with _mock_request(return_value={"id": 42, "options": {}}):
            result = await _call(mcp, "datadog_unmute_monitor", {"monitor_id": 42})

    assert result["muted"] is False


@pytest.mark.asyncio
async def test_search_logs_returns_entries_and_cursor(mcp) -> None:
    upstream = {
        "data": [
            {"id": "ev1", "attributes": {"timestamp": "2026-11-02T00:00:00Z", "message": "boom", "status": "error", "service": "api", "tags": ["env:prod"]}},
        ],
        "meta": {"page": {"after": "cursor-2"}},
    }
    async with _with_credentials():
        with _mock_request(return_value=upstream):
            result = await _call(mcp, "datadog_search_logs", {"query": "status:error", "time_from": "now-15m", "time_to": "now"})

    assert len(result["events"]) == 1
    assert result["events"][0]["message"] == "boom"
    assert result["cursor"] == "cursor-2"


@pytest.mark.asyncio
async def test_search_logs_empty_result_is_not_an_error(mcp) -> None:
    async with _with_credentials():
        with _mock_request(return_value={"data": [], "meta": {"page": {}}}):
            result = await _call(mcp, "datadog_search_logs", {"query": "status:error", "time_from": "now-15m", "time_to": "now"})

    assert result["events"] == []
    assert "error" not in result


@pytest.mark.asyncio
async def test_search_events_empty_result_is_not_an_error(mcp) -> None:
    async with _with_credentials():
        with _mock_request(return_value={"data": [], "meta": {"page": {}}}):
            result = await _call(mcp, "datadog_search_events", {"query": "source:deploy", "time_from": "now-1h", "time_to": "now"})

    assert result["events"] == []
    assert "error" not in result


@pytest.mark.asyncio
async def test_query_metrics_returns_timeseries_points(mcp) -> None:
    upstream = {"series": [{"metric": "system.cpu.user", "scope": "*", "unit": None, "pointlist": [[1700000000000, 42.0]]}], "query": "avg:system.cpu.user{*}"}
    async with _with_credentials():
        with _mock_request(return_value=upstream):
            result = await _call(mcp, "datadog_query_metrics", {"query": "avg:system.cpu.user{*}", "time_from": 1699999000, "time_to": 1700000000})

    assert result["series"][0]["pointlist"] == [[1700000000000, 42.0]]


@pytest.mark.asyncio
async def test_query_metrics_maps_400_to_invalid_argument(mcp) -> None:
    async with _with_credentials():
        with _mock_request(side_effect=UpstreamError(400, "bad query")):
            result = await _call(mcp, "datadog_query_metrics", {"query": "not a valid query(", "time_from": 1, "time_to": 2})

    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_list_active_metrics_truncates_with_has_more(mcp) -> None:
    names = [f"metric.{i}" for i in range(60)]
    async with _with_credentials():
        with _mock_request(return_value={"metrics": names}):
            result = await _call(mcp, "datadog_list_active_metrics", {"time_from": 1700000000, "limit": 50})

    assert len(result["metrics"]) == 50
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_list_active_metrics_prefix_filter(mcp) -> None:
    names = ["system.cpu.user", "system.mem.used", "aws.ec2.cpu"]
    async with _with_credentials():
        with _mock_request(return_value={"metrics": names}):
            result = await _call(mcp, "datadog_list_active_metrics", {"time_from": 1700000000, "prefix": "system."})

    assert result["metrics"] == ["system.cpu.user", "system.mem.used"]


def test_missing_site_header_defaults_to_us1() -> None:
    assert resolve_base_url(None) == "https://api.datadoghq.com"
    assert resolve_base_url("") == "https://api.datadoghq.com"


def test_site_header_selects_eu_base_url() -> None:
    assert resolve_base_url("datadoghq.eu") == "https://api.datadoghq.eu"


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.content = json.dumps(self._body).encode() if body is not None else b""
        self.headers = headers or {}

    def json(self):
        return self._body


@pytest.mark.asyncio
async def test_rate_limited_response_is_retried() -> None:
    responses = [_FakeResponse(429, headers={"Retry-After": "0"}), _FakeResponse(200, {"monitors": []})]

    async def fake_request(method, url, params=None, json=None):
        return responses.pop(0)

    client = DatadogClient(api_key="k", application_key="a")
    client._client.request = fake_request  # instance attribute, no implicit self-binding
    try:
        body = await client.request("GET", "/api/v1/monitor/search")
    finally:
        await client.aclose()

    assert body == {"monitors": []}


@pytest.mark.asyncio
async def test_exhausted_retries_return_upstream_error() -> None:
    async def fake_request(method, url, params=None, json=None):
        return _FakeResponse(503, {"errors": ["service unavailable"]})

    client = DatadogClient(api_key="k", application_key="a")
    client._client.request = fake_request
    try:
        with pytest.raises(UpstreamError) as excinfo:
            await client.request("GET", "/api/v1/monitor/search")
    finally:
        await client.aclose()

    assert excinfo.value.status_code == 503
