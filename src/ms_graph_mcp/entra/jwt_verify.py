"""Entra ID JWT verification → :class:`Principal`.

Two paths, switched by ``AuthConfig.verify_signature``:

  1. **Verified** (default): fetch Entra's JWKS for the tenant, verify the RS256
     signature, audience, and expiry (with clock-skew leeway), then validate the
     issuer against the v1 + v2 tenant URLs ourselves (PyJWT's exact-match
     ``issuer=`` is brittle across token versions / trailing slashes).
  2. **Unverified** (local dev only): decode without signature, still enforce
     **audience**, expiry and issuer. Never reachable when a role or scope gate
     is configured.

Turning off signature verification narrows *one* check. It must not silently
drop the audience gate too: validating ``aud`` needs no signing key, and it is
the confused-deputy mitigation the MCP authorization spec requires on every
request. Without it, a token minted for Microsoft Graph — or for any other
service in the tenant — would be accepted here.

ID tokens (those carrying a ``nonce``) are always rejected — only access tokens
may be presented to a service.
"""

from __future__ import annotations

import time

import jwt
from jwt import PyJWKClient, PyJWKClientError

from ms_graph_mcp.entra.claims import Principal, extract_principal
from ms_graph_mcp.entra.config import AuthConfig
from ms_graph_mcp.entra.errors import InvalidTokenError, MissingTokenError

# One PyJWKClient per JWKS URL — its built-in cache means we fetch the key set
# once and refresh on ``lifespan``; an unknown ``kid`` (key rotation) triggers a
# refetch automatically.
_jwks_clients: dict[str, PyJWKClient] = {}


def get_jwks_client(url: str, lifespan: int = 3600) -> PyJWKClient:
    client = _jwks_clients.get(url)
    if client is None:
        client = PyJWKClient(url, cache_keys=True, lifespan=lifespan)
        _jwks_clients[url] = client
    return client


def _decode_verified(token: str, cfg: AuthConfig) -> dict:
    issuers = cfg.effective_issuers
    if not issuers:
        raise InvalidTokenError("tenant/issuer not configured for verification")

    try:
        client = get_jwks_client(cfg.effective_jwks_url, cfg.jwks_cache_ttl_seconds)
        signing_key = client.get_signing_key_from_jwt(token)
    except PyJWKClientError as exc:
        raise InvalidTokenError(f"JWKS lookup failed: {exc}") from exc
    except Exception as exc:  # network / parse errors from the JWKS endpoint
        raise InvalidTokenError(f"JWKS client error: {exc}") from exc

    audiences = cfg.audience_list
    try:
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=(audiences or None),
            leeway=cfg.clock_skew_seconds,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": bool(audiences),
                "verify_iss": False,  # validated explicitly below (version-robust)
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidTokenError("token expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise InvalidTokenError("audience mismatch") from exc
    except jwt.InvalidTokenError as exc:
        raise InvalidTokenError(f"token verification failed: {exc}") from exc

    if payload.get("iss", "") not in issuers:
        raise InvalidTokenError("issuer not recognized for the configured tenant")
    return payload


def _decode_unverified(token: str, cfg: AuthConfig) -> dict:
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError as exc:
        raise InvalidTokenError(f"malformed token: {exc}") from exc

    exp = payload.get("exp")
    if exp and time.time() > float(exp) + cfg.clock_skew_seconds:
        raise InvalidTokenError("token expired")

    # Audience is enforced on this path too. It costs nothing without the key,
    # and skipping it would mean a token issued for another service — Graph, say
    # — was accepted here purely because signature checking was off for local
    # development. `aud` may be a string or a list.
    audiences = cfg.audience_list
    if audiences:
        claimed = payload.get("aud", "")
        claimed_set = {claimed} if isinstance(claimed, str) else set(claimed or ())
        if not claimed_set & set(audiences):
            raise InvalidTokenError("audience mismatch")

    issuers = cfg.effective_issuers
    iss = payload.get("iss", "")
    if issuers and iss and iss not in issuers:
        raise InvalidTokenError("issuer not recognized for the configured tenant")
    return payload


def verify_token(token: str, cfg: AuthConfig) -> Principal:
    """Verify ``token`` per ``cfg`` and return the :class:`Principal`.

    Raises :class:`MissingTokenError` / :class:`InvalidTokenError` on failure.
    """
    if not token:
        raise MissingTokenError("empty bearer token")

    claims = (
        _decode_verified(token, cfg) if cfg.verify_signature else _decode_unverified(token, cfg)
    )

    if "nonce" in claims:
        raise InvalidTokenError("ID token presented where an access token is required")

    return extract_principal(claims)
