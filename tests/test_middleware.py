"""Credential middleware: missing-header 401, context isolation across concurrent requests."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from datadog_mcp.server import build_app, current_credentials


@asynccontextmanager
async def _running_app(app):
    """Drive the ASGI lifespan protocol by hand so the MCP session manager's
    task group is actually started -- exactly the SOP section 7 pitfall
    (Mount() does not propagate a sub-app's lifespan) this wiring exists to
    avoid. Without this, any request that reaches the mounted MCP app fails
    with "Task group is not initialized."
    """
    to_app: asyncio.Queue = asyncio.Queue()
    from_app: asyncio.Queue = asyncio.Queue()

    async def receive():
        return await to_app.get()

    async def send(message):
        await from_app.put(message)

    task = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await to_app.put({"type": "lifespan.startup"})
    message = await from_app.get()
    assert message["type"] == "lifespan.startup.complete", message
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        message = await from_app.get()
        assert message["type"] == "lifespan.shutdown.complete", message
        await task


def _initialize_body() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }


async def _post_mcp(client: httpx.AsyncClient, headers: dict[str, str], body: dict) -> httpx.Response:
    merged = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **headers}
    return await client.post("/mcp", headers=merged, json=body)


@pytest.mark.asyncio
async def test_missing_required_header_returns_401_with_header_name() -> None:
    app = build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _post_mcp(client, {"X-Datadog-Api-Key": "k1"}, _initialize_body())

    assert response.status_code == 401
    payload = response.json()
    assert payload["error"]["code"] == "unauthorized"
    assert "X-Datadog-Application-Key" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_health_endpoint_does_not_require_credentials() -> None:
    app = build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_leak_credentials() -> None:
    """Two concurrent initialize calls with different credentials must each see only their own."""
    seen: dict[str, str] = {}

    app = build_app()

    async def call_with(tenant: str, api_key: str) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await _post_mcp(
                client,
                {"X-Datadog-Api-Key": api_key, "X-Datadog-Application-Key": f"{api_key}-app"},
                _initialize_body(),
            )
            seen[tenant] = response.status_code

    async with _running_app(app):
        await asyncio.gather(call_with("tenant-a", "key-a"), call_with("tenant-b", "key-b"))

    # Both initialize calls succeeded independently -- if credentials leaked across the
    # concurrent requests this would still return 200s, so the real assertion is in
    # test_context_var_isolation below, which inspects the ContextVar directly.
    assert seen == {"tenant-a": 200, "tenant-b": 200}


@pytest.mark.asyncio
async def test_context_var_isolation_between_concurrent_middleware_invocations() -> None:
    """Directly exercise the ContextVar to prove one request's middleware instance
    never observes another's credentials, even when both are in flight at once."""
    from datadog_mcp.server import CredentialsMiddleware

    observed: list[str] = []

    async def inner_app(scope, receive, send):
        if scope["type"] == "http":
            # Simulate work that yields control, giving the other concurrent
            # request a chance to run before this one reads its credentials.
            await asyncio.sleep(0.01)
            observed.append(current_credentials().api_key)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

    middleware = CredentialsMiddleware(inner_app, mcp_path="/mcp")

    async def run(api_key: str) -> None:
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [
                (b"x-datadog-api-key", api_key.encode()),
                (b"x-datadog-application-key", b"app-key"),
            ],
        }

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        await middleware(scope, receive, send)

    await asyncio.gather(run("key-a"), run("key-b"))

    assert sorted(observed) == ["key-a", "key-b"]
