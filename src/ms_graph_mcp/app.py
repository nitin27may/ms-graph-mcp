"""graph-mcp Streamable-HTTP transport.

Builds a Starlette app that mounts the MCP Streamable HTTP transport at ``/mcp``
and exposes an unauthenticated ``/health`` probe. Usable standalone (run via the
``ms-graph-mcp-http`` console script) or wrapped by the in-fleet agent, which
injects the app's telemetry + the shared-secret config.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator, Callable

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from ms_graph_mcp.allowlists import READ_TOOL_NAMES
from ms_graph_mcp.auth import GraphMcpAuthMiddleware
from ms_graph_mcp.config import GraphMcpConfig, get_config, set_config
from ms_graph_mcp.server import build_graph_mcp_server

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8094
SERVICE_NAME = "ms-graph-mcp"


def build_app(
    cfg: GraphMcpConfig | None = None,
    *,
    setup_telemetry: Callable[[str], None] | None = None,
    instrument_starlette: Callable[[Starlette], None] | None = None,
) -> Starlette:
    """Build a fresh graph-mcp Starlette app.

    A factory (not a module-level singleton) so tests can spin up independent
    instances — ``StreamableHTTPSessionManager.run()`` may only be entered once
    per manager.

    ``cfg`` overrides the active package config. ``setup_telemetry`` /
    ``instrument_starlette`` are optional hooks the in-fleet wrapper supplies to
    wire OTEL; standalone runs leave them ``None``.
    """
    if cfg is not None:
        set_config(cfg)
    active = get_config()

    mcp_server = build_graph_mcp_server()
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        json_response=True,
        stateless=True,
    )

    async def health(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": SERVICE_NAME,
                "tools": len(READ_TOOL_NAMES),
            }
        )

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        if setup_telemetry is not None:
            setup_telemetry(SERVICE_NAME)
        if instrument_starlette is not None:
            instrument_starlette(app)
        async with session_manager.run():
            logger.info("graph-mcp ready — %d read tools", len(READ_TOOL_NAMES))
            yield

    application = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Mount("/mcp", app=handle_mcp),
        ],
        lifespan=lifespan,
    )
    application.add_middleware(GraphMcpAuthMiddleware, config=active.to_auth_config())
    return application


def run() -> None:
    """Console entry point (``ms-graph-mcp-http``) — run the HTTP server."""
    import uvicorn

    port = int(os.getenv("GRAPH_MCP_PORT", str(DEFAULT_PORT)))
    uvicorn.run(build_app(), host="0.0.0.0", port=port, log_level="info")
