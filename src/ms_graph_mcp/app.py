"""graph-mcp Streamable-HTTP transport.

Builds a Starlette app that serves the MCP Streamable HTTP transport at ``/mcp``
and exposes an unauthenticated ``/health`` probe. Usable standalone (run via the
``ms-graph-mcp-http`` console script) or embedded in another application, which
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

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ms_graph_mcp.allowlists import READ_TOOL_NAMES
from ms_graph_mcp.auth import GraphMcpAuthMiddleware
from ms_graph_mcp.config import GraphMcpConfig, get_config, set_config
from ms_graph_mcp.logging_setup import configure_logging
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


def _allowed_origins(cfg: GraphMcpConfig) -> list[str]:
    """Origins accepted by the transport, mirroring the allowed hosts.

    Only browser clients send `Origin`; a mismatch is a 403. Both schemes are
    listed for each host because TLS is usually terminated at the proxy, so the
    scheme the browser used is not the one this process sees.
    """
    origins: list[str] = []
    for host in cfg.allowed_hosts_list:
        origins += [f"http://{host}", f"https://{host}"]
    return origins


def _discovery_routes(cfg: GraphMcpConfig) -> list[Route]:
    """RFC 9728 protected-resource metadata, when a public URL is configured.

    Uses the SDK's own implementation rather than hand-rolling the document, so
    the shape tracks the spec as the SDK does. Only the routes are borrowed —
    the SDK's bearer middleware is deliberately not adopted, because
    ``GraphMcpAuthMiddleware`` additionally handles the shared-secret machine
    principal, the write scope and the internal tier, none of which the SDK
    knows about. Swapping enforcement would risk those for no gain in
    discovery.

    Without ``GRAPH_MCP_RESOURCE_URL`` there is nothing truthful to publish —
    the server cannot know its own public URL behind a proxy — so discovery is
    simply off and the transport behaves exactly as before.
    """
    if not cfg.resource_url:
        return []
    from mcp.server.auth.routes import create_protected_resource_routes
    from pydantic import AnyHttpUrl

    return list(
        create_protected_resource_routes(
            resource_url=AnyHttpUrl(cfg.resource_url),
            authorization_servers=[AnyHttpUrl(cfg.authorization_server)],
            scopes_supported=cfg.scopes_list or None,
            resource_name=SERVICE_NAME,
        )
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
    ``instrument_starlette`` are optional hooks an embedding application supplies to
    wire OTEL; standalone runs leave them ``None``.
    """
    if cfg is not None:
        set_config(cfg)
    active = get_config()

    mcp_server = build_graph_mcp_server()

    # `/health` is served by the MCP app itself rather than a wrapper, so the
    # auth middleware below covers both routes and there is exactly one app.
    routes = [Route("/health", _health, methods=["GET"])]
    routes.extend(_discovery_routes(active))

    application = mcp_server.streamable_http_app(
        # Explicit, because the SDK's inferred default is wrong here: it keys
        # DNS-rebinding protection off the `host` argument, and the default
        # 127.0.0.1 makes it trust localhost alone. This process binds 0.0.0.0
        # (containers), so behind an ingress every request would arrive with a
        # real hostname and be refused with 421 before reaching any handler.
        transport_security=TransportSecuritySettings(
            allowed_hosts=active.allowed_hosts_list,
            allowed_origins=_allowed_origins(active),
        ),
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_REQUEST_BODY_SIZE,
        custom_starlette_routes=routes,
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

    level = configure_logging()
    port = int(os.getenv("GRAPH_MCP_PORT", str(DEFAULT_PORT)))
    # uvicorn's access log follows the same setting rather than being pinned to
    # info, so raising the level actually quietens the process.
    uvicorn.run(build_app(), host="0.0.0.0", port=port, log_level=level.lower())
