"""The validated ``Principal`` and claim extraction.

``Principal`` is the framework-agnostic result of verifying a token: who the
caller is (user or app), their email, tenant, App Roles, and the authorized
party (``azp``/``appid``). Tools and downstream code read this instead of
re-parsing the raw JWT.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Principal:
    """A verified caller identity extracted from an Entra ID access token."""

    subject_id: str  # oid (preferred) or sub
    email: str  # preferred_username / upn / email, lowercased ("" for app-only)
    tenant_id: str  # tid
    roles: frozenset[str]  # App Roles from the `roles` claim
    azp: str  # authorized party — azp (v2) or appid (v1)
    is_app_only: bool  # client-credentials token (no user) vs delegated
    # S2 (agentic audit) — distinct from is_app_only. Any real Entra
    # client-credentials token is_app_only=True too, so callers that need
    # "this is specifically the trusted fleet machine-secret bypass, not
    # just some app-only caller" (e.g. the MCP internal/deterministic tier
    # gate) must check this field, never is_app_only. Only
    # middleware._machine_principal sets it True; extract_principal (real
    # verified JWTs) never does.
    is_machine: bool = False
    raw: dict = field(default_factory=dict, repr=False)


def extract_principal(claims: dict) -> Principal:
    """Build a :class:`Principal` from a decoded (already-verified) JWT payload."""
    email = (
        (
            claims.get("preferred_username")
            or claims.get("upn")
            or claims.get("email")
            or ""
        )
        .strip()
        .lower()
    )

    raw_roles = claims.get("roles") or []
    if isinstance(raw_roles, str):
        raw_roles = [raw_roles]

    azp = (claims.get("azp") or claims.get("appid") or "").strip()
    subject_id = (claims.get("oid") or claims.get("sub") or "").strip()
    tenant_id = (claims.get("tid") or "").strip()

    # App-only (client-credentials) tokens carry idtyp="app" in v2; older tokens
    # are detected by the absence of both a user identity and delegated scopes.
    is_app_only = claims.get("idtyp") == "app" or (not email and not claims.get("scp"))

    return Principal(
        subject_id=subject_id,
        email=email,
        tenant_id=tenant_id,
        roles=frozenset(raw_roles),
        azp=azp,
        is_app_only=is_app_only,
        raw=claims,
    )
