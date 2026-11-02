"""FastMCP factory, credential middleware, and the ASGI app.

Three contract requirements live here (vendor-mcp-development-sop.md
section 1.3 / 3.3 / 7):

1. The server is truly stateless (`stateless_http=True`) -- no session
   affinity is assumed anywhere else in this codebase.
2. Credentials are read per-request from headers into a `ContextVar` and
   reset in a `finally` block -- never stored in a global or module-level
   singleton (that would leak one tenant's credentials into another
   tenant's concurrent request).
3. DNS-rebinding protection is disabled, because this service sits behind
   an internal reverse proxy that does not forward `localhost` as the
   Host header; leaving it on makes every real request 421.
"""

from __future__ import annotations

import contextvars
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from datadog_mcp.config import settings
from datadog_mcp.api_client import DatadogClient

API_KEY_HEADER = "X-Datadog-Api-Key"
APPLICATION_KEY_HEADER = "X-Datadog-Application-Key"
SITE_HEADER = "X-Datadog-Site"

_REQUIRED_HEADERS = (API_KEY_HEADER, APPLICATION_KEY_HEADER)

INSTRUCTIONS = """\
This service exposes Datadog's monitors, logs, metrics, and events to an
AI agent investigating infrastructure and application health.

Typical flow: search or list monitors to find what is alerting, pull a
specific monitor's detail for its definition and state, then cross-check
the logs and events around the same time window to understand why. Query
metrics directly when the question is about a specific timeseries rather
than an alert. Use `datadog_mute_monitor` / `datadog_unmute_monitor` only
when asked to silence or restore a specific named monitor -- these are
the only write operations this service exposes; nothing here creates,
edits, or deletes a monitor definition.

All list/search tools are paginated (`limit`, plus a `page` or `cursor`);
check `has_more` before assuming a result set is complete.
"""


@dataclass(frozen=True)
class RequestCredentials:
    api_key: str
    application_key: str
    site: str | None


_credentials: contextvars.ContextVar[RequestCredentials] = contextvars.ContextVar("datadog_credentials")


def current_credentials() -> RequestCredentials:
    """Raises LookupError if called outside a request that passed the middleware."""
    return _credentials.get()


def client_factory() -> DatadogClient:
    """Build a fresh, request-scoped Datadog client from the current ContextVar.

    A new client is built on every call -- nothing here is cached across
    requests (vendor-mcp-development-sop.md section 3.4).
    """
    creds = current_credentials()
    return DatadogClient(api_key=creds.api_key, application_key=creds.application_key, site=creds.site)


class CredentialsMiddleware:
    """Pure ASGI middleware: reads credential headers, 401s if any are missing.

    Deliberately not a `BaseHTTPMiddleware` -- that buffers the entire
    response, which breaks the Streamable HTTP transport's SSE responses.
    Only applies to the MCP endpoint; `/health` is untouched.
    """

    def __init__(self, app, mcp_path: str) -> None:
        self.app = app
        self._mcp_path = mcp_path

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] != self._mcp_path:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        values = {name: headers.get(name) for name in _REQUIRED_HEADERS}
        missing = [name for name, value in values.items() if not value]
        if missing:
            response = JSONResponse(
                {
                    "error": {
                        "code": "unauthorized",
                        "message": f"Missing required header(s): {', '.join(missing)}",
                        "retryable": False,
                    }
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        token = _credentials.set(
            RequestCredentials(
                api_key=values[API_KEY_HEADER],
                application_key=values[APPLICATION_KEY_HEADER],
                site=headers.get(SITE_HEADER),
            )
        )
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials.reset(token)


def build_mcp() -> FastMCP:
    mcp = FastMCP(
        "datadog-mcp",
        instructions=INSTRUCTIONS,
        host=settings.mcp_http_host,
        port=settings.mcp_http_port,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    from datadog_mcp.tools import events, logs, metrics, monitors

    monitors.register(mcp, client_factory)
    logs.register(mcp, client_factory)
    metrics.register(mcp, client_factory)
    events.register(mcp, client_factory)

    return mcp


async def health(_request) -> JSONResponse:
    """Pure local probe -- never calls the Datadog API (SOP section 1.1)."""
    return JSONResponse({"status": "ok"})


def build_app() -> Starlette:
    mcp = build_mcp()
    mcp_asgi_app = mcp.streamable_http_app()  # creates mcp.session_manager as a side effect

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        # Mount() does not propagate a sub-app's lifespan (SOP section 7) --
        # the session manager must be started/stopped by the outer app.
        async with mcp.session_manager.run():
            yield

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Mount("/", app=mcp_asgi_app),
        ],
        lifespan=lifespan,
    )
    return CredentialsMiddleware(app, mcp_path=mcp.settings.streamable_http_path)
