"""Service auth for the graph-mcp Streamable-HTTP transport.

Auth is delegated to the bundled ``ms_graph_mcp.entra`` toolkit (DOWNSTREAM posture):

- **Tool calls** carry the user's OBO Graph token in ``Authorization`` — it is
  validated as a real Entra JWT (signature when enabled, audience = Graph, and
  ``azp`` matching the configured client id, so only tokens minted by this app
  registration are accepted).
- **No-user calls** (the agent's startup ``tools/list`` hydration) carry the
  shared secret in ``Authorization`` and take the machine bypass.

The validated principal + the MCP-specific headers (``X-Write-Scope`` and the
optional ``X-Entra-App-Token``) are assembled into ``current_request_context``,
the dict the MCP dispatch handlers read. The previous bespoke shared-secret /
``X-Graph-Token`` logic is gone — token verification now lives in the package.
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ms_graph_mcp.context import current_request_context
from ms_graph_mcp.entra import AuthConfig, AuthMode
from ms_graph_mcp.entra.context import current_access_token
from ms_graph_mcp.entra.errors import AuthError
from ms_graph_mcp.entra.middleware import authenticate_request

logger = logging.getLogger(__name__)

# /health is unauthenticated so container and orchestrator probes work.
_PUBLIC_PATHS = frozenset({"/health"})

# OAuth discovery documents MUST be reachable without a token — a client that
# has none is exactly who needs to read them. Serving RFC 9728 metadata behind
# the very authentication it describes makes the endpoint useless.
_PUBLIC_PATH_PREFIXES = ("/.well-known/",)


def _www_authenticate() -> str:
    """The Bearer challenge, pointing at the metadata document.

    Empty when no public URL is configured — a pointer to a document that is
    not served would be worse than none.
    """
    from ms_graph_mcp.config import get_config

    cfg = get_config()
    metadata_url = cfg.resource_metadata_url
    if not metadata_url:
        return ""
    parts = [f'Bearer resource_metadata="{metadata_url}"']
    if cfg.scopes_list:
        parts.append(f'scope="{" ".join(cfg.scopes_list)}"')
    return ", ".join(parts)


class GraphMcpAuthMiddleware(BaseHTTPMiddleware):
    """Validate the inbound token (or accept the machine bypass) and stash the
    per-request Graph credentials into ``current_request_context``."""

    def __init__(self, app, *, config: AuthConfig) -> None:
        super().__init__(app)
        self._cfg = config

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PATH_PREFIXES):
            return await call_next(request)

        try:
            principal = await authenticate_request(
                request, cfg=self._cfg, mode=AuthMode.DOWNSTREAM_SERVICE
            )
        except AuthError as exc:
            logger.warning("ms-graph-mcp: rejected request (%s)", exc.reason)
            headers = {}
            # RFC 9728 / MCP authorization: a 401 carrying a resource_metadata
            # pointer is what lets a spec-compliant client discover how to
            # authenticate on its own. Without it the client only knows it was
            # refused, and every integration needs bespoke configuration.
            if exc.status_code == 401:
                challenge = _www_authenticate()
                if challenge:
                    headers["WWW-Authenticate"] = challenge
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code, headers=headers)

        # A machine/no-user call (shared-secret bypass) carries no Graph token —
        # leave access_token empty so dispatch fail-closes on any tool call that
        # actually needs Graph (only tools/list hydration uses this path).
        graph_token = "" if principal.is_app_only else current_access_token.get("")

        # Internal (deterministic) tier — for our own ETL/workers/REST/sinks calling
        # the MCP as plain functions, NOT the LLM-agent surface. Unlocked ONLY for the
        # machine-secret principal (``is_machine``) plus ``X-Internal-Scope: true``.
        # Agents and external MCP clients present USER tokens (not app-only), so they
        # can never reach it. In this mode the caller either acts for a user via
        # ``X-OBO-Token`` (dispatch OBOs it) or omits it for an app-only operation
        # (dispatch mints client-credentials).
        #
        # S2 (agentic audit) — this used to gate on ``is_app_only``, which is
        # also True for any REAL verified Entra client-credentials token, not
        # just the machine-secret bypass. ``is_machine`` is set ONLY by
        # ``_machine_principal`` (the shared-secret path); a real app-only JWT
        # verified via ``extract_principal`` never sets it. Defense in depth:
        # ``authenticate_request`` now also rejects real app-only tokens at
        # this DOWNSTREAM_SERVICE edge unless ``allow_app_only`` is configured
        # (default False for both MCP postures), so this check is not the
        # only thing standing between an app-only token and the internal tier.
        internal_scope = principal.is_machine and (
            request.headers.get("X-Internal-Scope", "").lower() == "true"
        )
        obo_token = request.headers.get("X-OBO-Token", "").strip() if internal_scope else ""

        ctx: dict = {
            # In internal mode the user assertion (if any) rides X-OBO-Token; the
            # agent path keeps using the forwarded/validated token.
            "access_token": obo_token or graph_token,
            "user_email": principal.email,
            "write_scope": request.headers.get("X-Write-Scope", "").lower() == "true",
            "internal_scope": internal_scope,
        }
        # Narrowing only — server.py intersects this with the startup ceiling,
        # so an untrusted caller cannot reach a namespace the deployment did not
        # enable.
        requested_toolsets = request.headers.get("X-Toolsets", "").strip()
        if requested_toolsets:
            ctx["toolsets"] = requested_toolsets

        entra_app_token = request.headers.get("X-Entra-App-Token", "")
        if entra_app_token:
            ctx["entra_app_token"] = entra_app_token
        current_request_context.set(ctx)

        return await call_next(request)
