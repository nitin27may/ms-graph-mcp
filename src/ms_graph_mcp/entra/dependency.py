"""FastAPI-compatible auth dependency.

Wraps the same :func:`authenticate_request` core as the middleware, mapping
:class:`AuthError` to a Starlette ``HTTPException`` (which FastAPI renders). Use
when a route needs the verified :class:`Principal` injected, rather than the
blanket middleware. No hard FastAPI import — works via ``Depends`` on any
Starlette-based app.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.exceptions import HTTPException
from starlette.requests import Request

from ms_graph_mcp.entra.claims import Principal
from ms_graph_mcp.entra.config import AuthConfig, get_config
from ms_graph_mcp.entra.errors import AuthError
from ms_graph_mcp.entra.middleware import authenticate_request
from ms_graph_mcp.entra.presets import AuthMode
from ms_graph_mcp.entra.service_auth import ServiceAuthVerifier


def build_auth_dependency(
    *,
    config: AuthConfig | None = None,
    mode: AuthMode = AuthMode.AGENT_EDGE,
    service_verifier: ServiceAuthVerifier | None = None,
) -> Callable[[Request], Awaitable[Principal]]:
    """Return an async ``Depends``-able that verifies the request and returns the
    :class:`Principal`, raising ``HTTPException`` on failure."""

    async def _dependency(request: Request) -> Principal:
        cfg = config or get_config()
        try:
            return await authenticate_request(
                request, cfg=cfg, mode=mode, service_verifier=service_verifier
            )
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    return _dependency
