"""Service-to-service (machine identity) verification seam.

**Disabled by default.** The current posture is *network isolation*: agents and
MCP servers are reachable only via the gateway / private VNet, so "the caller is
a fleet service" is enforced by where a request can physically originate. The
re-validated user/OBO token is the sole per-call credential.

This module keeps the seam so a stronger machine check (Entra Managed Identity /
mTLS) can drop in later with **no call-site changes** — see
``docs/adr/NNNN-service-to-service-identity.md``. To enable, pass a verifier to
``ServiceAuthMiddleware(service_verifier=...)``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from starlette.requests import Request


@runtime_checkable
class ServiceAuthVerifier(Protocol):
    """Proves the *caller machine* is a trusted fleet service (not the user)."""

    async def verify(self, request: Request) -> bool:
        """Return True if the caller is an authorized fleet service."""
        ...


class ManagedIdentityVerifier:
    """Placeholder for future Entra Managed Identity / mTLS service auth.

    Not implemented — instantiating it is a hard error so it can't be wired in
    by accident before the ADR's upgrade path is built. See
    ``docs/adr/NNNN-service-to-service-identity.md``.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        # TODO(ADR service-to-service-identity): implement MI token validation
        # (caller presents its own MI access token; verify aud/appid against the
        # fleet allowlist) when network isolation is no longer sufficient.
        raise NotImplementedError(
            "Managed Identity service auth is not yet implemented; the current "
            "posture is network isolation. See the service-to-service-identity ADR."
        )
