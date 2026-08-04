"""``ServiceAuthMiddleware`` — the single auth seam for agents and MCP servers.

Per request (skipping configured public paths) it runs:

  1. **service-auth** (optional, disabled by default — network isolation),
  2. **token verification** (JWKS RS256 + iss/aud/exp),
  3. **azp allowlist** (when configured — downstream services),
  4. **App-Role gate** (AGENT_EDGE only).

The shared core is :func:`authenticate_request`, reused by the FastAPI
dependency so both entry points apply identical checks.
"""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ms_graph_mcp.entra.authz import check_roles
from ms_graph_mcp.entra.claims import Principal
from ms_graph_mcp.entra.config import AuthConfig, get_config
from ms_graph_mcp.entra.context import (
    current_access_token,
    current_principal,
    current_user_email,
)
from ms_graph_mcp.entra.errors import (
    AppOnlyError,
    AuthError,
    AzpError,
    MissingTokenError,
    RoleError,
    ServiceAuthError,
)
from ms_graph_mcp.entra.jwt_verify import verify_token
from ms_graph_mcp.entra.presets import AuthMode
from ms_graph_mcp.entra.service_auth import ServiceAuthVerifier
from ms_graph_mcp.entra.telemetry import AuditHook, default_audit


def _machine_principal(request: Request) -> Principal:
    """Principal for a trusted fleet machine call (shared-secret bypass).

    Identity comes from the ``X-User-Email`` header (the inter-service caller
    passes the acting user, or the ``agent`` sentinel for no-user calls).
    """
    email = (request.headers.get("X-User-Email") or "agent").strip().lower()
    return Principal(
        subject_id="",
        email=email,
        tenant_id="",
        roles=frozenset(),
        azp="",
        is_app_only=True,
        is_machine=True,
        raw={"idtyp": "machine"},
    )


def _bind_context(request: Request, token: str, principal: Principal) -> None:
    current_access_token.set(token)
    current_user_email.set(principal.email)
    current_principal.set(principal)
    try:
        request.state.user_email = principal.email
        request.state.principal = principal
    except Exception:  # pragma: no cover — request.state always present in ASGI
        pass


async def authenticate_request(
    request: Request,
    *,
    cfg: AuthConfig,
    mode: AuthMode,
    service_verifier: ServiceAuthVerifier | None = None,
) -> Principal:
    """Run the full auth chain. Returns the :class:`Principal` or raises
    :class:`AuthError` (which carries ``status_code`` + ``reason``)."""
    # 1. service-auth (machine identity) — only when a verifier is wired.
    if service_verifier is not None and not await service_verifier.verify(request):
        raise ServiceAuthError("service authentication failed")

    # 2. token verification
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise MissingTokenError("Missing Authorization header")
    token = auth_header.removeprefix("Bearer ").strip()

    # 2a. Machine-identity bypass — a fleet service presenting the shared secret
    # is trusted (network-isolation posture); skip user-token verification + the
    # role gate. Constant-time compare; never matches an empty secret/token.
    # This is the documented stopgap until Managed Identity (see ADR).
    if cfg.shared_secret and token and hmac.compare_digest(token, cfg.shared_secret):
        machine = _machine_principal(request)
        _bind_context(request, token, machine)
        return machine

    principal = verify_token(token, cfg)  # raises Missing/InvalidTokenError

    # 3. azp allowlist (downstream services restrict to our app's OBO tokens)
    allowed_azp = cfg.allowed_azp_set
    if allowed_azp and principal.azp not in allowed_azp:
        raise AzpError(f"caller application '{principal.azp}' is not permitted")

    # 4. App-only policy — both postures. S2 (agentic audit) — this used to
    # be inside the `mode == AGENT_EDGE` branch only, so a DOWNSTREAM_SERVICE
    # (MCP) never rejected a real Entra app-only (client-credentials) token
    # even when cfg.allow_app_only was False. Any app registration in the
    # tenant granted an azp-allowlisted / correctly-audienced role could
    # present itself as app-only and reach the MCP surface. Legitimate
    # app-only Graph/ADO access in this fleet already goes through the
    # machine-secret bypass + internal tier, never a raw app-only JWT
    # presented directly — so this has no legitimate caller to break.
    if principal.is_app_only and not cfg.allow_app_only:
        raise AppOnlyError("app-only tokens are not permitted for this service")

    # 5. App-Role gate — agent edge only. The OBO token's audience is generic
    # across apps, so azp (step 3) is what restricts DOWNSTREAM_SERVICE's
    # surface, not a role claim — see AuthMode's docstring.
    if mode == AuthMode.AGENT_EDGE:
        required = cfg.required_roles_set
        if not check_roles(principal, required, cfg.allow_app_only):
            raise RoleError("caller lacks a required role")

    _bind_context(request, token, principal)
    return principal


class ServiceAuthMiddleware(BaseHTTPMiddleware):
    """ASGI middleware applying :func:`authenticate_request` to every request."""

    def __init__(
        self,
        app,
        *,
        config: AuthConfig | None = None,
        mode: AuthMode = AuthMode.AGENT_EDGE,
        service_verifier: ServiceAuthVerifier | None = None,
        audit_hook: AuditHook | None = None,
    ) -> None:
        super().__init__(app)
        self.cfg = config or get_config()
        self.mode = mode
        self.service_verifier = service_verifier
        self.audit = audit_hook or default_audit

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self.cfg.public_paths_set:
            return await call_next(request)

        try:
            principal = await authenticate_request(
                request,
                cfg=self.cfg,
                mode=self.mode,
                service_verifier=self.service_verifier,
            )
        except AuthError as exc:
            self.audit("audit.auth_failure", {"reason": exc.reason, "path": path})
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)

        self.audit(
            "audit.auth_success",
            {"path": path, "user": principal.email, "app_only": principal.is_app_only},
        )
        return await call_next(request)
