"""On-Behalf-Of token exchange for graph-mcp (resource-server posture, D4).

When graph-mcp runs as an OAuth resource server (``mcp_does_obo``), it receives a
token audienced to **itself** and must exchange it for a Microsoft Graph token via
the OAuth 2.0 On-Behalf-Of flow before calling Graph. This module performs that
exchange with MSAL.

MSAL's ``ConfidentialClientApplication`` keeps an in-process token cache keyed on a
hash of the user assertion + scopes, so back-to-back tool calls within one request
reuse the cached Graph token without a network round-trip. A cross-replica L2 cache
(Redis) is a deliberate non-goal here — it would drag a redis dependency into a
publishable, dependency-light package; each MCP process caching in-memory is the
right scope.

Mirrors ``backend/shared/graph_auth._obo_exchange`` but self-contained (the package
must not import the host app's ``shared`` modules).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading

logger = logging.getLogger(__name__)


class OboError(RuntimeError):
    """Raised when the OBO exchange cannot be performed or is rejected.

    Carries the parts of Entra's rejection a caller can act on. ``claims`` is the
    one that matters: when Conditional Access demands a step-up (MFA, sign-in
    frequency), Entra answers the exchange with a *claims challenge* rather than a
    token, and the client must acquire a new token satisfying it. Dropping that
    value leaves the user stuck in a loop with no way to complete the step-up, so
    it is carried out to the transport rather than flattened into a log line.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "",
        suberror: str = "",
        claims: str = "",
        correlation_id: str = "",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.suberror = suberror
        self.claims = claims
        self.correlation_id = correlation_id

    @property
    def requires_interaction(self) -> bool:
        """Whether the user can resolve this by authenticating again.

        A claims challenge is the definitive signal; ``interaction_required`` is
        the error code Entra pairs it with. Anything else — a misconfigured
        secret, an unauthorized client — is not something re-authentication
        fixes, and telling the client to retry would just spin.
        """
        return bool(self.claims) or self.error_code == "interaction_required"


# One long-lived ConfidentialClientApplication per (tenant, client) so MSAL's
# internal token cache survives across requests. Guarded for thread-safety since
# the exchange runs in a thread-pool executor.
_apps: dict[tuple[str, str], object] = {}
_apps_lock = threading.Lock()


def _get_app(tenant_id: str, client_id: str, client_secret: str):
    import msal

    key = (tenant_id, client_id)
    with _apps_lock:
        app = _apps.get(key)
        if app is None:
            app = msal.ConfidentialClientApplication(
                client_id=client_id,
                client_credential=client_secret,
                authority=f"https://login.microsoftonline.com/{tenant_id}",
            )
            _apps[key] = app
        return app


async def acquire_token_on_behalf_of(
    user_token: str,
    scopes: list[str],
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> str:
    """Exchange the inbound user token for a Microsoft Graph token via OBO.

    Raises :class:`OboError` if credentials are missing or Entra rejects the
    exchange — the dispatch path surfaces this as a structured tool error rather
    than calling Graph unauthenticated.
    """
    if not scopes:
        raise OboError("no OBO scopes configured")
    if not (tenant_id and client_id and client_secret):
        raise OboError("OBO not configured (tenant_id / client_id / client_secret required)")

    loop = asyncio.get_event_loop()

    def _sync() -> str:
        try:
            import msal  # noqa: F401  (import guard — surfaces a clear error)
        except ImportError as exc:  # pragma: no cover - msal is a declared dep
            raise OboError("msal is not installed — OBO unavailable") from exc

        app = _get_app(tenant_id, client_id, client_secret)
        result = app.acquire_token_on_behalf_of(user_assertion=user_token, scopes=scopes)
        token = result.get("access_token")
        if token:
            return token

        error = result.get("error", "unknown")
        desc = result.get("error_description", "")
        correlation_id = result.get("correlation_id", "")
        claims = result.get("claims", "") or ""
        suberror = result.get("suberror", "") or ""
        logger.error(
            "graph-mcp OBO failed: error=%s suberror=%s correlation_id=%s claims=%s desc=%s scopes=%s",
            error,
            suberror,
            correlation_id,
            bool(claims),
            desc[:300],
            scopes,
        )
        raise OboError(
            f"OBO exchange failed ({error}): {desc[:200]}",
            error_code=error,
            suberror=suberror,
            claims=claims,
            correlation_id=correlation_id,
        )

    return await loop.run_in_executor(None, functools.partial(_sync))


async def acquire_token_for_client(
    scopes: list[str],
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> str:
    """Acquire an app-only token via the client-credentials grant.

    Used by the internal tier's app-only operations (e.g. the access-revalidation
    probe) where there is no user to act for — the MCP authenticates as itself.
    MSAL caches the result in-process. Raises :class:`OboError` on misconfig /
    rejection so dispatch fails closed rather than calling unauthenticated.
    """
    if not scopes:
        raise OboError("no client-credentials scopes configured")
    if not (tenant_id and client_id and client_secret):
        raise OboError(
            "client credentials not configured (tenant_id / client_id / client_secret required)"
        )

    loop = asyncio.get_event_loop()

    def _sync() -> str:
        try:
            import msal  # noqa: F401  (import guard — surfaces a clear error)
        except ImportError as exc:  # pragma: no cover - msal is a declared dep
            raise OboError("msal is not installed — client credentials unavailable") from exc

        app = _get_app(tenant_id, client_id, client_secret)
        result = app.acquire_token_for_client(scopes=scopes)
        token = result.get("access_token")
        if token:
            return token

        error = result.get("error", "unknown")
        desc = result.get("error_description", "")
        logger.error(
            "graph-mcp client-credentials failed: error=%s desc=%s scopes=%s",
            error,
            desc[:300],
            scopes,
        )
        raise OboError(f"client-credentials acquisition failed ({error}): {desc[:200]}")

    return await loop.run_in_executor(None, functools.partial(_sync))
