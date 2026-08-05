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

import base64
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


def _www_authenticate(*, error: str = "", claims: str = "") -> str:
    """The Bearer challenge, pointing at the metadata document.

    With ``claims``, this is also the Conditional Access step-up channel.
    Microsoft's OBO guidance is explicit: when the exchange fails because a
    policy needs satisfying, the middle tier answers 401 and puts the claims
    challenge in this header, and the client acquires a fresh token presenting
    it. Dropping the challenge makes MFA step-up impossible — the client only
    learns it was refused, retries the same token, and fails identically.

    Returns empty only when there is nothing useful to say: no public URL *and*
    no challenge. A pointer to a document that is not served would be worse
    than none.
    """
    from ms_graph_mcp.config import get_config

    cfg = get_config()
    metadata_url = cfg.resource_metadata_url
    if not (metadata_url or claims or error):
        return ""

    parts = ["Bearer"] if not metadata_url else [f'Bearer resource_metadata="{metadata_url}"']
    if error:
        parts.append(f'error="{error}"')
    if cfg.scopes_list:
        parts.append(f'scope="{" ".join(cfg.scopes_list)}"')
    if claims:
        # Base64url per the Microsoft/OpenID claims-challenge convention: the
        # raw value is JSON and would otherwise break header quoting.
        encoded = base64.urlsafe_b64encode(claims.encode("utf-8")).decode("ascii").rstrip("=")
        parts.append(f'claims="{encoded}"')
    return ", ".join(parts)


async def _exchange_for_graph_token(user_token: str) -> str:
    """Run the on-behalf-of exchange for the resource-server posture.

    Lives here rather than in dispatch for two reasons. Dispatch is shared with
    the stdio transport, whose token is already a Graph token that Entra will
    not redeem; and a claims challenge is only actionable as an HTTP response,
    which dispatch cannot produce.

    MSAL caches on (assertion, scopes), so this is one network round-trip per
    session rather than per tool call.
    """
    from ms_graph_mcp.config import get_config
    from ms_graph_mcp.obo import CredentialError, OboError, acquire_token_on_behalf_of

    cfg = get_config()
    try:
        credential = cfg.client_credential
    except CredentialError as exc:
        raise OboError(f"client credential unusable: {exc}") from exc

    return await acquire_token_on_behalf_of(
        user_token,
        cfg.obo_scopes_list,
        tenant_id=cfg.tenant_id,
        client_id=cfg.client_id,
        credential=credential or "",
    )


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

        # In internal mode the user assertion (if any) rides X-OBO-Token; the
        # agent path uses the validated inbound token.
        access_token = obo_token or graph_token

        # Resource-server posture: the inbound token is audienced to *this*
        # server, so it is useless against Graph until exchanged. Do it here,
        # while an HTTP response can still be shaped — a Conditional Access
        # claims challenge is only actionable as a 401 carrying the challenge.
        #
        # Skipped for the machine principal: it either supplied a downstream
        # token explicitly via X-OBO-Token or is doing an app-only operation
        # that dispatch mints client credentials for.
        from ms_graph_mcp.config import get_config

        if get_config().mcp_does_obo and access_token and not principal.is_machine:
            from ms_graph_mcp.obo import OboError

            try:
                access_token = await _exchange_for_graph_token(access_token)
            except OboError as exc:
                return self._obo_failure_response(exc)

        ctx: dict = {
            "access_token": access_token,
            "user_email": principal.email,
            "write_scope": self._write_scope(request, principal),
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

    @staticmethod
    def _write_scope(request: Request, principal) -> bool:
        """Whether write tools are permitted for this request.

        ``X-Write-Scope`` is a value the caller sets for itself, so on its own it
        is a *preference*, not authority. When the deployment names a write scope
        (``GRAPH_MCP_WRITE_SCOPE_NAME``), the token's ``scp`` claim decides and
        the header may only narrow — an agent that was never granted the write
        scope cannot reach ``mail_send`` however it sets its headers.

        With no write scope configured the header is all there is, which is the
        pre-existing behaviour and why the setting defaults empty. The machine
        principal presents a shared secret rather than a user token and has no
        ``scp``, so it keeps the header semantics too.
        """
        from ms_graph_mcp.config import get_config

        asked = request.headers.get("X-Write-Scope", "").lower() == "true"
        if not asked:
            return False

        write_scope_name = get_config().write_scope_name.strip()
        if not write_scope_name or principal.is_machine:
            return True
        return write_scope_name in principal.scopes

    def _obo_failure_response(self, exc) -> JSONResponse:
        """Turn a failed exchange into something the client can act on.

        A Conditional Access step-up is not an error the caller can fix by
        retrying — it needs a *new* token carrying the claims Entra asked for.
        Answering 401 with the challenge is what lets a client do that; the
        previous behaviour buried it in a 200 tool result, where nothing could
        act on it and MFA step-up simply could not complete.

        Anything else is a server-side configuration fault and stays a 500: a
        401 there would send the client into a re-authorization loop against a
        problem no token can solve.
        """
        if exc.needs_user_interaction:
            logger.warning(
                "ms-graph-mcp: OBO needs user interaction (%s, correlation_id=%s)",
                exc.error_code or "unknown",
                exc.correlation_id or "-",
            )
            challenge = _www_authenticate(
                error=exc.error_code or "invalid_token", claims=exc.claims
            )
            headers = {"WWW-Authenticate": challenge} if challenge else {}
            return JSONResponse(
                {
                    "error": "interaction_required",
                    "error_description": str(exc),
                    "correlation_id": exc.correlation_id,
                },
                status_code=401,
                headers=headers,
            )

        logger.error("ms-graph-mcp: OBO exchange failed (%s)", exc)
        return JSONResponse(
            {"error": "obo_failed", "error_description": str(exc)},
            status_code=500,
        )
