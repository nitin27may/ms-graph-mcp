"""graph-mcp Streamable-HTTP transport.

Builds a Starlette app that serves the MCP Streamable HTTP transport at ``/mcp``
and exposes an unauthenticated ``/health`` probe. Usable standalone (run via the
``ms-graph-mcp-http`` console script) or wrapped by a host application, which
injects its telemetry + the shared-secret config.

MCP SDK 2.x note: the low-level ``Server`` now builds the Starlette app itself
via ``streamable_http_app()``, including running the session manager in its own
lifespan. The previous manual ``StreamableHTTPSessionManager`` wiring is gone.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ms_graph_mcp.allowlists import READ_TOOL_NAMES
from ms_graph_mcp.auth import GraphMcpAuthMiddleware
from ms_graph_mcp.config import GraphMcpConfig, get_config, set_config
from ms_graph_mcp.server import build_graph_mcp_server

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8094
SERVICE_NAME = "ms-graph-mcp"

# Streamable HTTP rejects oversized bodies with HTTP 413. The SDK default is
# 4 MiB, which the internal tier's base64 file-upload tool can exceed on a
# modest attachment — base64 inflates by about a third, so 4 MiB of wire budget
# is only ~3 MiB of file. 16 MiB keeps ordinary documents working; anything
# larger belongs on a chunked upload session, not a single JSON-RPC frame.
MAX_REQUEST_BODY_SIZE = 16 * 1024 * 1024


async def _health(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": SERVICE_NAME,
            "tools": len(READ_TOOL_NAMES),
        }
    )


def build_app(
    cfg: GraphMcpConfig | None = None,
    *,
    setup_telemetry: Callable[[str], None] | None = None,
    instrument_starlette: Callable[[Starlette], None] | None = None,
) -> Starlette:
    """Build a fresh graph-mcp Starlette app.

    A factory (not a module-level singleton) so tests can spin up independent
    instances — the session manager's ``run()`` may only be entered once per
    manager.

    ``cfg`` overrides the active package config. ``setup_telemetry`` /
    ``instrument_starlette`` are optional hooks a host application supplies to
    wire OTEL; standalone runs leave them ``None``.
    """
    if cfg is not None:
        set_config(cfg)
    active = get_config()

    mcp_server = build_graph_mcp_server()

    # `/health` is served by the MCP app itself rather than a wrapper, so the
    # auth middleware below covers both routes and there is exactly one app.
    application = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_REQUEST_BODY_SIZE,
        custom_starlette_routes=[Route("/health", _health, methods=["GET"])],
    )

    if setup_telemetry is not None or instrument_starlette is not None:
        # streamable_http_app() owns the app's lifespan (it runs the session
        # manager there). Chain onto it rather than replacing it, or the
        # transport never starts.
        inner_lifespan = application.router.lifespan_context

        @contextlib.asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncIterator[None]:
            if setup_telemetry is not None:
                setup_telemetry(SERVICE_NAME)
            if instrument_starlette is not None:
                instrument_starlette(app)
            async with inner_lifespan(app):
                logger.info("graph-mcp ready — %d read tools", len(READ_TOOL_NAMES))
                yield

        application.router.lifespan_context = lifespan

    application.add_middleware(GraphMcpAuthMiddleware, config=active.to_auth_config())
    return application


def run() -> None:
    """Console entry point (``ms-graph-mcp-http``) — run the HTTP server."""
    import uvicorn

    port = int(os.getenv("GRAPH_MCP_PORT", str(DEFAULT_PORT)))
    uvicorn.run(build_app(), host="0.0.0.0", port=port, log_level="info")
