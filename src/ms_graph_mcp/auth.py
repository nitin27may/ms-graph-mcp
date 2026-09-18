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

**The resource-server OBO exchange happens here**, not in ``dispatch_graph_tool``.
Two reasons, both structural. A Conditional Access step-up arrives as a claims
challenge, and it can only reach the client as a ``401`` with a
``WWW-Authenticate`` header — a tool result is always an HTTP 200, so a challenge
placed there is sealed inside a body no client acts on and the step-up can never
complete. And dispatch is shared with stdio, where the inbound token is *already*
a Graph token and exchanging it breaks every call; keeping the exchange in HTTP
middleware means stdio has no code path to it at all, however the server is
configured (``tests/test_stdio_unaffected.py``).
"""

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ms_graph_mcp.context import current_request_context
from ms_graph_mcp.entra import AuthConfig, AuthMode
from ms_graph_mcp.entra.context import current_access_token
from ms_graph_mcp.entra.errors import AuthError
from ms_graph_mcp.entra.middleware import authenticate_request

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from ms_graph_mcp.obo import OboError

logger = logging.getLogger(__name__)

# /health is unauthenticated so container and orchestrator probes work.
_PUBLIC_PATHS = frozenset({"/health"})

# OAuth discovery documents MUST be reachable without a token — a client that
# has none is exactly who needs to read them. Serving RFC 9728 metadata behind
# the very authentication it describes makes the endpoint useless.
_PUBLIC_PATH_PREFIXES = ("/.well-known/",)


def _www_authenticate(*, error: str = "", claims: str = "") -> str:
    """The Bearer challenge, pointing at the metadata document.

    ``error`` and ``claims`` carry an OAuth error code and a Conditional Access
    claims challenge. The claims value is base64-encoded because it is raw JSON
    from Entra and a bare ``{"access_token":{...}}`` in a header value would not
    survive parsing.

    Without a configured public URL there is no metadata pointer — but a
    challenge that names an error or carries claims is still worth sending, so
    only the plain discovery form degrades to empty.
    """
    from ms_graph_mcp.config import get_config

    cfg = get_config()
    metadata_url = cfg.resource_metadata_url
    if not metadata_url and not error and not claims:
        # Plain discovery challenge with nothing to point at. A pointer to a
        # document that is not served would be worse than no header.
        return ""
    parts: list[str] = []
    if error:
        parts.append(f'error="{error}"')
    if claims:
        encoded = base64.b64encode(claims.encode()).decode()
        parts.append(f'claims="{encoded}"')
    if metadata_url:
        parts.append(f'resource_metadata="{metadata_url}"')
    if cfg.scopes_list:
        parts.append(f'scope="{" ".join(cfg.scopes_list)}"')
    if not parts:
        return ""
    return "Bearer " + ", ".join(parts)


async def _jsonrpc_method(request: Request) -> str:
    """The JSON-RPC method of an MCP request, or ``""`` if there isn't one.

    The middleware has to know whether a request is a ``tools/call`` before it
    can decide anything token-related: exchanging a token for ``initialize`` or
    ``tools/list`` would add a round-trip to every handshake and let a failure
    that only matters to Graph refuse a listing that has nothing to do with it.

    Reading the body here is safe — Starlette's ``BaseHTTPMiddleware`` caches it
    and replays it downstream, so the transport still sees the full request.
    Protocol revision 2026-07-28 removed JSON-RPC batching, so one request
    object is the whole surface. Anything unparseable is reported as not a tool
    call and left for the transport to reject properly.
    """
    if request.method != "POST":
        return ""
    try:
        payload = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    method = payload.get("method")
    return method if isinstance(method, str) else ""


def _obo_error_response(exc: OboError) -> JSONResponse:
    """Turn a failed OBO exchange into a response the client can act on.

    Two outcomes, because they need different things from the caller:

    - **The user can fix it by signing in again** — Conditional Access wants MFA
      or a fresher sign-in. Answer ``401`` with the claims challenge, which is
      what Microsoft's guidance prescribes for a middle tier and what makes the
      client acquire a new token and retry.
    - **Nothing the caller does will help** — a rejected assertion, a
      misconfigured credential. Answer ``502``: this server could not reach its
      own upstream. A ``401`` here would send a client round the sign-in loop
      for a problem that is not theirs.
    """
    if exc.requires_interaction:
        logger.warning("ms-graph-mcp: OBO needs interaction (%s)", exc.error_code or "claims")
        headers = {}
        challenge = _www_authenticate(
            error=exc.error_code or "insufficient_claims", claims=exc.claims
        )
        if challenge:
            headers["WWW-Authenticate"] = challenge
        return JSONResponse({"error": str(exc)}, status_code=401, headers=headers)

    logger.error("ms-graph-mcp: OBO exchange failed (%s)", exc.error_code or "unknown")
    return JSONResponse({"error": str(exc)}, status_code=502)


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

        if await _jsonrpc_method(request) == "tools/call" and ctx["access_token"]:
            error_response = await self._exchange_for_graph_token(ctx)
            if error_response is not None:
                return error_response

        current_request_context.set(ctx)

        return await call_next(request)

    async def _exchange_for_graph_token(self, ctx: dict) -> JSONResponse | None:
        """Resource-server OBO: swap the inbound token for a Graph token in place.

        Returns ``None`` on success (``ctx`` is updated) or the response to send
        instead. It lives here rather than in ``dispatch_graph_tool`` for two
        reasons. A Conditional Access step-up arrives as a claims challenge, and
        a tool result is an HTTP 200 — the challenge would be sealed inside a
        JSON-RPC body no client acts on, so MFA step-up could never complete.
        And dispatch is shared with stdio, where the inbound token is already a
        Graph token and an exchange breaks every call; keeping this in HTTP
        middleware means stdio has no path to it at all.
        """
        from ms_graph_mcp.config import get_config
        from ms_graph_mcp.obo import OboError, acquire_token_on_behalf_of

        cfg = get_config()
        if not cfg.mcp_does_obo:
            return None

        try:
            ctx["access_token"] = await acquire_token_on_behalf_of(
                ctx["access_token"],
                cfg.obo_scopes_list,
                tenant_id=cfg.tenant_id,
                client_id=cfg.client_id,
                client_secret=cfg.client_secret,
            )
        except OboError as exc:
            return _obo_error_response(exc)
        return None
