"""On-Behalf-Of token exchange for graph-mcp (resource-server posture).

When graph-mcp runs as an OAuth resource server, it receives a token audienced
to **itself** and must exchange it for a Microsoft Graph token via the OAuth 2.0
On-Behalf-Of flow before calling Graph. This module performs that exchange with
MSAL.

The exchange is driven from ``auth.py`` — the HTTP middleware — and never from
dispatch. That is deliberate: dispatch is shared with the stdio transport, whose
token is *already* a Graph token, and Entra refuses to redeem a token audienced
to another app. Keeping the exchange on the HTTP edge also means a Conditional
Access claims challenge can still be turned into a real ``401``, which is the
only place a client can act on it.

MSAL's ``ConfidentialClientApplication`` keeps an in-process token cache keyed on
a hash of the user assertion + scopes, so back-to-back tool calls within one
session reuse the cached Graph token without a network round-trip. A
cross-replica L2 cache (Redis) is a deliberate non-goal — it would drag a redis
dependency into a publishable, dependency-light package; each MCP process caching
in-memory is the right scope.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Errors where Entra is telling the *user* to do something — satisfy MFA, accept
# a Conditional Access policy, grant consent — rather than reporting a broken
# configuration. These are the ones that carry a claims challenge and must be
# surfaced to the client as a 401 so it can re-authorize; retrying the same
# assertion will never succeed.
INTERACTION_ERRORS = frozenset(
    {"interaction_required", "consent_required", "login_required", "invalid_grant"}
)


class OboError(RuntimeError):
    """Raised when the OBO exchange cannot be performed or is rejected.

    Carries the fields the caller needs to build an RFC 6750 challenge.
    ``claims`` is the Conditional Access claims challenge, present when Entra
    wants the user to satisfy a policy (MFA, sign-in frequency, a compliant
    device). Microsoft's guidance is explicit: the middle tier replies 401 with
    ``WWW-Authenticate`` carrying this value, and the client acquires a new
    token presenting it. Swallowing it makes step-up impossible.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "",
        claims: str = "",
        correlation_id: str = "",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.claims = claims
        self.correlation_id = correlation_id

    @property
    def needs_user_interaction(self) -> bool:
        """Whether the caller should re-authorize rather than retry or give up."""
        return bool(self.claims) or self.error_code in INTERACTION_ERRORS


# ── Client credentials ────────────────────────────────────────────────────────


class CredentialError(ValueError):
    """The configured client credential cannot be assembled."""


def _certificate_credential(cert_path: str, password: str) -> dict:
    """Build MSAL's certificate credential from a PEM file.

    The file is the output of ``openssl pkcs12 -in file.pfx -out file.pem
    -nodes`` — private key and certificate concatenated. Both are passed:
    MSAL 1.35+ derives an **SHA-256** thumbprint when the certificate is present
    and no thumbprint is given, which avoids asking the operator to paste the
    SHA-1 one Entra shows in the portal and which MSAL has deprecated.
    """
    path = Path(cert_path).expanduser()
    try:
        pem = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CredentialError(f"cannot read certificate at {path}: {exc}") from exc

    key_start = pem.find("-----BEGIN")
    cert_start = pem.find("-----BEGIN CERTIFICATE-----")
    if key_start == -1 or cert_start == -1:
        raise CredentialError(
            f"{path} does not look like a PEM bundle — it must contain both the private key "
            "and the certificate. Convert a .pfx with: "
            "openssl pkcs12 -in file.pfx -out file.pem -nodes"
        )

    credential: dict = {
        "private_key": pem[:cert_start].strip(),
        "public_certificate": pem[cert_start:].strip(),
    }
    if password:
        credential["passphrase"] = password
    return credential


def _federated_credential(token_file: str) -> dict:
    """Build MSAL's client-assertion credential from a projected token file.

    A **callable**, not a string. AKS workload identity rotates the projected
    service-account token, and a pre-signed assertion read once would expire
    somewhere in the middle of a long-running process. MSAL invokes this only
    when it actually needs to go on the wire, so the read is rare and the
    assertion is always current.
    """
    path = Path(token_file).expanduser()

    def read_assertion() -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:  # pragma: no cover - surfaced at exchange time
            raise CredentialError(f"cannot read federated token at {path}: {exc}") from exc

    if not path.exists():
        raise CredentialError(
            f"federated token file {path} does not exist. On AKS this is projected by the "
            "workload-identity webhook — check the pod has the azure.workload.identity/use label."
        )
    return {"client_assertion": read_assertion}


def build_client_credential(
    *,
    client_secret: str = "",
    cert_path: str = "",
    cert_password: str = "",
    federated_token_file: str = "",
) -> str | dict | None:
    """Resolve the configured credential into MSAL's ``client_credential``.

    Precedence is **certificate → federated → secret**, most secure first, so a
    leftover secret in the environment cannot silently take priority over a
    deliberately configured certificate. Returns ``None`` when nothing is
    configured; the caller decides whether that is fatal.
    """
    if cert_path:
        return _certificate_credential(cert_path, cert_password)
    if federated_token_file:
        return _federated_credential(federated_token_file)
    if client_secret:
        return client_secret
    return None


# One long-lived ConfidentialClientApplication per (tenant, client, credential
# kind) so MSAL's internal token cache survives across requests. A federated
# credential is a callable, which MSAL re-invokes per token request, so a
# rotating assertion does not require rebuilding the app. Guarded for
# thread-safety since the exchange runs in a thread-pool executor.
_apps: dict[tuple[str, str, str], object] = {}
_apps_lock = threading.Lock()


def _credential_kind(credential: str | dict) -> str:
    if isinstance(credential, str):
        return "secret"
    if "client_assertion" in credential:
        return "federated"
    return "certificate"


def _get_app(tenant_id: str, client_id: str, credential: str | dict):
    import msal

    key = (tenant_id, client_id, _credential_kind(credential))
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


def _raise_from_result(result: dict, scopes: list[str], operation: str) -> None:
    """Turn a failed MSAL result into an :class:`OboError` that carries its claims."""
    error = result.get("error", "unknown")
    desc = result.get("error_description", "")
    correlation_id = result.get("correlation_id", "")
    claims = result.get("claims", "") or ""

    logger.error(
        "graph-mcp %s failed: error=%s correlation_id=%s claims=%s desc=%s scopes=%s",
        operation,
        error,
        correlation_id,
        bool(claims),
        desc[:300],
        scopes,
    )
    raise OboError(
        f"{operation} failed ({error}): {desc[:200]}",
        error_code=error,
        claims=claims,
        correlation_id=correlation_id,
    )


async def acquire_token_on_behalf_of(
    user_token: str,
    scopes: list[str],
    *,
    tenant_id: str,
    client_id: str,
    credential: str | dict,
) -> str:
    """Exchange the inbound user token for a Microsoft Graph token via OBO.

    Raises :class:`OboError` if credentials are missing or Entra rejects the
    exchange. When the rejection carries a claims challenge, the error carries it
    too, so the middleware can answer with a 401 the client can act on.
    """
    if not scopes:
        raise OboError("no OBO scopes configured")
    if not (tenant_id and client_id and credential):
        raise OboError("OBO not configured (tenant_id / client_id / a credential required)")

    loop = asyncio.get_event_loop()

    def _sync() -> str:
        try:
            import msal  # noqa: F401  (import guard — surfaces a clear error)
        except ImportError as exc:  # pragma: no cover - msal is a declared dep
            raise OboError("msal is not installed — OBO unavailable") from exc

        app = _get_app(tenant_id, client_id, credential)
        result = app.acquire_token_on_behalf_of(user_assertion=user_token, scopes=scopes)
        token = result.get("access_token")
        if token:
            return token
        _raise_from_result(result, scopes, "OBO exchange")
        raise AssertionError("unreachable")  # pragma: no cover

    return await loop.run_in_executor(None, functools.partial(_sync))


async def acquire_token_for_client(
    scopes: list[str],
    *,
    tenant_id: str,
    client_id: str,
    credential: str | dict,
) -> str:
    """Acquire an app-only token via the client-credentials grant.

    Used by the internal tier's app-only operations (e.g. the access-revalidation
    probe) where there is no user to act for — the MCP authenticates as itself.
    MSAL caches the result in-process. Raises :class:`OboError` on misconfig /
    rejection so dispatch fails closed rather than calling unauthenticated.
    """
    if not scopes:
        raise OboError("no client-credentials scopes configured")
    if not (tenant_id and client_id and credential):
        raise OboError(
            "client credentials not configured (tenant_id / client_id / a credential required)"
        )

    loop = asyncio.get_event_loop()

    def _sync() -> str:
        try:
            import msal  # noqa: F401  (import guard — surfaces a clear error)
        except ImportError as exc:  # pragma: no cover - msal is a declared dep
            raise OboError("msal is not installed — client credentials unavailable") from exc

        app = _get_app(tenant_id, client_id, credential)
        result = app.acquire_token_for_client(scopes=scopes)
        token = result.get("access_token")
        if token:
            return token
        _raise_from_result(result, scopes, "client-credentials acquisition")
        raise AssertionError("unreachable")  # pragma: no cover

    return await loop.run_in_executor(None, functools.partial(_sync))
