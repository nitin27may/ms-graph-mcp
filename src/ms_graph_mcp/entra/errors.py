"""Auth error hierarchy.

Each error carries an HTTP ``status_code`` (401 authn / 403 authz) and a short
``reason`` slug used for audit events. The middleware maps the exception to a
JSON response; the FastAPI dependency maps it to an ``HTTPException``.
"""

from __future__ import annotations


class AuthError(Exception):
    """Base class for all authentication / authorization failures."""

    status_code: int = 401
    reason: str = "auth_error"


class MissingTokenError(AuthError):
    """No bearer token present on the request."""

    reason = "missing_bearer"


class InvalidTokenError(AuthError):
    """Token failed signature / issuer / audience / expiry validation."""

    reason = "invalid_token"


class ServiceAuthError(AuthError):
    """The caller failed the (optional) service-to-service machine check."""

    reason = "service_auth"


class AuthorizationError(AuthError):
    """Authenticated, but not permitted (403)."""

    status_code = 403
    reason = "authz_denied"


class AzpError(AuthorizationError):
    """The token's authorized party (azp/appid) is not in the allowlist."""

    reason = "azp_mismatch"


class RoleError(AuthorizationError):
    """The caller lacks a required App Role."""

    reason = "role_denied"


class AppOnlyError(AuthorizationError):
    """An app-only token was presented to a service that only allows users."""

    reason = "app_only_denied"


class InsufficientScopeError(AuthorizationError):
    """The token lacks a delegated scope the request needs.

    Distinct from :class:`RoleError` because the remedy is different and a
    client can act on it: OAuth defines ``insufficient_scope`` as a challenge,
    so a conforming client re-authorizes for the named scope and retries. A
    missing App Role is an administrative grant no client can obtain for itself.
    """

    reason = "insufficient_scope"

    def __init__(self, message: str, *, scope: str = "") -> None:
        super().__init__(message)
        self.scope = scope
        """The scope to name in the challenge, so the client knows what to ask for."""
