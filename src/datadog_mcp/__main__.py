"""Entry point: run the Datadog MCP service with uvicorn."""

from __future__ import annotations

import uvicorn

from datadog_mcp.config import settings
from datadog_mcp.server import build_app


def main() -> None:
    uvicorn.run(build_app(), host=settings.mcp_http_host, port=settings.mcp_http_port)


if __name__ == "__main__":
    main()
