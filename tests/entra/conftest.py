"""Shared fixtures: a real RS256 keypair, a fake JWKS client, and a token factory.

Tests sign tokens with a generated private key and monkeypatch
``jwt_verify.get_jwks_client`` to return the matching public key, so the real
``jwt.decode`` verification path is exercised without any network call.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ms_graph_mcp.entra import jwt_verify
from ms_graph_mcp.entra.config import reset_config

TENANT = "tenant-123"
CLIENT = "client-abc"
ISS_V2 = f"https://login.microsoftonline.com/{TENANT}/v2.0"
GRAPH_AUD = "https://graph.microsoft.com"


@pytest.fixture(autouse=True)
def _reset_state():
    reset_config()
    jwt_verify._jwks_clients.clear()
    yield
    reset_config()
    jwt_verify._jwks_clients.clear()


@pytest.fixture(scope="session")
def rsa_keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


@pytest.fixture
def make_token(rsa_keypair):
    priv, _pub = rsa_keypair

    def _make(remove: tuple[str, ...] = (), **overrides) -> str:
        now = int(time.time())
        payload = {
            "iss": ISS_V2,
            "aud": f"api://{CLIENT}",
            "exp": now + 3600,
            "iat": now,
            "nbf": now,
            "tid": TENANT,
            "oid": "user-oid-1",
            "preferred_username": "alice@example.com",
            "azp": CLIENT,
            "scp": "access_as_user",
            "roles": ["meeting-prep.user"],
        }
        payload.update(overrides)
        for key in remove:
            payload.pop(key, None)
        return jwt.encode(payload, priv, algorithm="RS256", headers={"kid": "test-key"})

    return _make


@pytest.fixture
def patched_jwks(rsa_keypair, monkeypatch):
    """Make jwt_verify resolve the signing key to our generated public key."""
    _priv, pub = rsa_keypair

    class _Key:
        def __init__(self, key):
            self.key = key

    class _Client:
        def __init__(self, *a, **k):
            pass

        def get_signing_key_from_jwt(self, token):
            return _Key(pub)

    monkeypatch.setattr(
        jwt_verify, "get_jwks_client", lambda url, lifespan=3600: _Client()
    )
    return pub
