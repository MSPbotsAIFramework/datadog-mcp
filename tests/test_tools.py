"""tools/list snapshot, per-domain success mapping, and error-code mapping.

All Datadog HTTP calls are mocked -- these tests never touch the network
(SOP section 10).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

import datadog_mcp.api_client as api_client_module
from datadog_mcp.api_client import DatadogClient, resolve_base_url
from datadog_mcp.errors import UpstreamError
from datadog_mcp.logging_utils import logger as datadog_logger, reset_request_id, set_request_id
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


@pytest.mark.asyncio
async def test_retry_loop_stops_once_time_budget_is_exhausted(monkeypatch) -> None:
    """Review round 1, finding 1: a transport that never responds must not be
    retried past the cumulative time budget, regardless of _MAX_RETRIES /
    _READ_TIMEOUT -- the whole call must stay well under the SOP's 120s
    per-call ceiling (vendor-mcp-development-sop.md section 5)."""
    monkeypatch.setattr(api_client_module, "_MAX_TOTAL_SECONDS", 0.3)

    async def never_returns(method, url, params=None, json=None):
        await asyncio.sleep(10)  # would hang well past 120s without the cap below

    client = DatadogClient(api_key="k", application_key="a")
    client._client.request = never_returns
    start = time.monotonic()
    try:
        with pytest.raises(UpstreamError) as excinfo:
            await client.request("GET", "/api/v1/monitor/search")
    finally:
        await client.aclose()
    elapsed = time.monotonic() - start

    assert excinfo.value.status_code == 599
    assert elapsed < 2.0, f"retry loop ran {elapsed:.1f}s past its 0.3s budget"


@pytest.mark.asyncio
async def test_upstream_call_emits_one_structured_log_line_without_credentials() -> None:
    """Review round 1, finding 2: every upstream call must log tool name,
    request id, upstream path, status code, latency and error code (SOP
    section 6) -- and never the credential header values."""

    captured: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Collect()
    datadog_logger.addHandler(handler)
    token = set_request_id("rid-test-1")
    try:
        client = DatadogClient(
            api_key="secret-key", application_key="secret-app-key", tool_name="datadog_get_monitor"
        )
        client._client.request = AsyncMock(return_value=_FakeResponse(200, {"id": 1}))
        try:
            await client.request("GET", "/api/v1/monitor/1")
        finally:
            await client.aclose()
    finally:
        reset_request_id(token)
        datadog_logger.removeHandler(handler)

    assert len(captured) == 1
    record = captured[0]
    assert record.tool == "datadog_get_monitor"
    assert record.request_id == "rid-test-1"
    assert record.path == "/api/v1/monitor/1"
    assert record.status_code == 200
    assert record.error_code is None
    assert record.latency_ms >= 0
    rendered = record.getMessage()
    assert "secret-key" not in rendered
    assert "secret-app-key" not in rendered


@pytest.mark.asyncio
async def test_upstream_call_log_line_records_mapped_error_code_on_failure() -> None:
    captured: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Collect()
    datadog_logger.addHandler(handler)
    try:

        async def fake_request(method, url, params=None, json=None):
            return _FakeResponse(404, {"errors": ["not found"]})

        client = DatadogClient(api_key="k", application_key="a", tool_name="datadog_get_monitor")
        client._client.request = fake_request
        try:
            with pytest.raises(UpstreamError):
                await client.request("GET", "/api/v1/monitor/999")
        finally:
            await client.aclose()
    finally:
        datadog_logger.removeHandler(handler)

    assert len(captured) == 1
    assert captured[0].status_code == 404
    assert captured[0].error_code == "not_found"
