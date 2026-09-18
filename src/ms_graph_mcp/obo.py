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
from pathlib import Path

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


# One long-lived ConfidentialClientApplication per (tenant, client, credential
# kind) so MSAL's internal token cache survives across requests. The credential
# kind is part of the key because the three kinds produce different apps; the
# credential *value* is not, so rotating a certificate on disk needs a restart
# (a federated token does not — see below). Guarded for thread-safety since the
# exchange runs in a thread-pool executor.
_apps: dict[tuple[str, str, str], object] = {}
_apps_lock = threading.Lock()


def _credential(
    client_secret: str = "",
    cert_path: str = "",
    cert_passphrase: str = "",
    federated_token_file: str = "",
) -> tuple[str, object]:
    """The MSAL ``client_credential``, and a name for the kind in force.

    Precedence is certificate → federated → secret, which is Microsoft's own
    order of preference: their Agent ID guidance says client secrets "shouldn't
    be used as client credentials in production environments". The secret stays
    supported because it is the only thing that works on a developer laptop.

    Returns ``("", None)`` when nothing is configured, so the caller can say so
    plainly instead of handing MSAL a credential it will reject later.
    """
    if cert_path:
        # MSAL wants a PEM holding the private key; passing the same bundle as
        # `public_certificate` lets it derive an SHA-256 thumbprint itself
        # (1.35.0+) rather than making the operator paste one from the portal.
        pem = Path(cert_path).read_text()
        credential: dict = {"private_key": pem, "public_certificate": pem}
        if cert_passphrase:
            credential["passphrase"] = cert_passphrase
        return "certificate", credential

    if federated_token_file:
        # A *callable*, not the token's current contents. Projected service
        # account tokens are rotated — AKS refreshes them roughly hourly — so a
        # value read once at startup works, then silently stops working. MSAL
        # invokes this only when it actually needs to go on the wire.
        def _assertion() -> str:
            return Path(federated_token_file).read_text().strip()

        return "federated", {"client_assertion": _assertion}

    if client_secret:
        return "secret", client_secret

    return "", None


def _get_app(tenant_id: str, client_id: str, kind: str, credential: object):
    import msal

    key = (tenant_id, client_id, kind)
    with _apps_lock:
        app = _apps.get(key)
        if app is None:
            app = msal.ConfidentialClientApplication(
                client_id=client_id,
                client_credential=credential,
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
    client_secret: str = "",
    cert_path: str = "",
    cert_passphrase: str = "",
    federated_token_file: str = "",
) -> str:
    """Exchange the inbound user token for a Microsoft Graph token via OBO.

    Raises :class:`OboError` if credentials are missing or Entra rejects the
    exchange — the dispatch path surfaces this as a structured tool error rather
    than calling Graph unauthenticated.
    """
    if not scopes:
        raise OboError("no OBO scopes configured")
    kind, credential = _credential(client_secret, cert_path, cert_passphrase, federated_token_file)
    if not (tenant_id and client_id and credential):
        raise OboError(
            "OBO not configured (tenant_id, client_id, and one of "
            "client_secret / cert_path / federated_token_file required)"
        )

    loop = asyncio.get_event_loop()

    def _sync() -> str:
        try:
            import msal  # noqa: F401  (import guard — surfaces a clear error)
        except ImportError as exc:  # pragma: no cover - msal is a declared dep
            raise OboError("msal is not installed — OBO unavailable") from exc

        app = _get_app(tenant_id, client_id, kind, credential)
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
    client_secret: str = "",
    cert_path: str = "",
    cert_passphrase: str = "",
    federated_token_file: str = "",
) -> str:
    """Acquire an app-only token via the client-credentials grant.

    Used by the internal tier's app-only operations (e.g. the access-revalidation
    probe) where there is no user to act for — the MCP authenticates as itself.
    MSAL caches the result in-process. Raises :class:`OboError` on misconfig /
    rejection so dispatch fails closed rather than calling unauthenticated.
    """
    if not scopes:
        raise OboError("no client-credentials scopes configured")
    kind, credential = _credential(client_secret, cert_path, cert_passphrase, federated_token_file)
    if not (tenant_id and client_id and credential):
        raise OboError(
            "client credentials not configured (tenant_id, client_id, and one of "
            "client_secret / cert_path / federated_token_file required)"
        )

    loop = asyncio.get_event_loop()

    def _sync() -> str:
        try:
            import msal  # noqa: F401  (import guard — surfaces a clear error)
        except ImportError as exc:  # pragma: no cover - msal is a declared dep
            raise OboError("msal is not installed — client credentials unavailable") from exc

        app = _get_app(tenant_id, client_id, kind, credential)
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
