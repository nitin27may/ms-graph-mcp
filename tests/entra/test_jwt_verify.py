"""Token verification: signature, audience, issuer, expiry, ID-token rejection."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ms_graph_mcp.entra.config import AuthConfig
from ms_graph_mcp.entra.errors import InvalidTokenError, MissingTokenError
from ms_graph_mcp.entra.jwt_verify import verify_token

# Must match the values baked into the make_token fixture (conftest.py).
TENANT = "tenant-123"
CLIENT = "client-abc"
ISS_V2 = f"https://login.microsoftonline.com/{TENANT}/v2.0"
GRAPH_AUD = "https://graph.microsoft.com"


def _cfg(**kw) -> AuthConfig:
    base = {"tenant_id": TENANT, "client_id": CLIENT, "jwt_verify": True}
    base.update(kw)
    return AuthConfig(**base)


def test_valid_token(make_token, patched_jwks):
    p = verify_token(make_token(), _cfg())
    assert p.email == "alice@example.com"
    assert "meeting-prep.user" in p.roles
    assert p.azp == CLIENT
    assert p.is_app_only is False


def test_expired_token_rejected(make_token, patched_jwks):
    now = int(time.time())
    tok = make_token(exp=now - 600, iat=now - 1200, nbf=now - 1200)
    with pytest.raises(InvalidTokenError):
        verify_token(tok, _cfg())


def test_wrong_audience_rejected(make_token, patched_jwks):
    with pytest.raises(InvalidTokenError):
        verify_token(make_token(aud="api://someone-else"), _cfg())


def test_wrong_issuer_rejected(make_token, patched_jwks):
    with pytest.raises(InvalidTokenError):
        verify_token(make_token(iss="https://evil.example/"), _cfg())


def test_id_token_with_nonce_rejected(make_token, patched_jwks):
    with pytest.raises(InvalidTokenError):
        verify_token(make_token(nonce="abc123"), _cfg())


def test_bad_signature_rejected(rsa_keypair, patched_jwks):
    # Sign with a different key than the JWKS returns → signature check fails.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    tok = jwt.encode(
        {
            "iss": ISS_V2,
            "aud": f"api://{CLIENT}",
            "exp": now + 3600,
            "tid": TENANT,
            "preferred_username": "alice@example.com",
            "scp": "access_as_user",
        },
        other,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    with pytest.raises(InvalidTokenError):
        verify_token(tok, _cfg())


def test_missing_token():
    with pytest.raises(MissingTokenError):
        verify_token("", _cfg())


def test_unverified_dev_path(make_token):
    # jwt_verify=False, no roles → unverified decode still validates exp + iss.
    p = verify_token(make_token(), _cfg(jwt_verify=False))
    assert p.email == "alice@example.com"


def test_unverified_dev_path_still_checks_expiry(make_token):
    now = int(time.time())
    tok = make_token(exp=now - 600)
    with pytest.raises(InvalidTokenError):
        verify_token(tok, _cfg(jwt_verify=False))


def test_graph_audience_token_for_downstream(make_token, patched_jwks):
    cfg = _cfg(client_id="", audience=GRAPH_AUD)
    p = verify_token(make_token(aud=GRAPH_AUD), cfg)
    assert p.azp == CLIENT
