"""Entra ID token validation + App-Role authorization middleware.

A self-contained, gateway-agnostic auth toolkit: JWKS-backed RS256 verification,
issuer / audience / expiry checks, principal extraction, an optional App-Role
gate, and a shared-secret bypass for machine-to-machine callers.

Public API:

    from ms_graph_mcp.entra import ServiceAuthMiddleware, AuthConfig, AuthMode

Two postures are supported (see ``presets.AuthMode``): AGENT_EDGE for a service
facing end users directly, and DOWNSTREAM_SERVICE for one sitting behind another
service, receiving an already-exchanged token. This MCP server runs
DOWNSTREAM_SERVICE — see ``ms_graph_mcp.auth`` for the policy layered on top.
"""

from __future__ import annotations

from ms_graph_mcp.entra.authz import check_roles
from ms_graph_mcp.entra.claims import Principal, extract_principal
from ms_graph_mcp.entra.config import AuthConfig, get_config, reset_config, set_config
from ms_graph_mcp.entra.context import (
    current_access_token,
    current_context_id,
    current_principal,
    current_user_email,
)
from ms_graph_mcp.entra.dependency import build_auth_dependency
from ms_graph_mcp.entra.errors import (
    AppOnlyError,
    AuthError,
    AuthorizationError,
    AzpError,
    InvalidTokenError,
    MissingTokenError,
    RoleError,
    ServiceAuthError,
)
from ms_graph_mcp.entra.jwt_verify import get_jwks_client, verify_token
from ms_graph_mcp.entra.middleware import ServiceAuthMiddleware, authenticate_request
from ms_graph_mcp.entra.presets import AuthMode
from ms_graph_mcp.entra.service_auth import ManagedIdentityVerifier, ServiceAuthVerifier
from ms_graph_mcp.entra.telemetry import AuditHook, default_audit

__all__ = [
    # config
    "AuthConfig",
    "get_config",
    "set_config",
    "reset_config",
    # claims
    "Principal",
    "extract_principal",
    # verification + authz
    "verify_token",
    "get_jwks_client",
    "check_roles",
    # middleware + dependency
    "ServiceAuthMiddleware",
    "authenticate_request",
    "build_auth_dependency",
    "AuthMode",
    # service-auth seam
    "ServiceAuthVerifier",
    "ManagedIdentityVerifier",
    # context
    "current_principal",
    "current_access_token",
    "current_user_email",
    "current_context_id",
    # telemetry
    "AuditHook",
    "default_audit",
    # errors
    "AuthError",
    "AuthorizationError",
    "MissingTokenError",
    "InvalidTokenError",
    "ServiceAuthError",
    "AzpError",
    "RoleError",
    "AppOnlyError",
]
