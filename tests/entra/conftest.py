"""Entra-toolkit fixtures.

The signed-token factory (``make_token`` / ``patched_jwks``) moved to the root
``tests/conftest.py`` so the posture tests at the ``build_app()`` level can use
it too; it is still available here, since root fixtures cascade down.
"""

from __future__ import annotations

import pytest

from ms_graph_mcp.entra import jwt_verify
from ms_graph_mcp.entra.config import reset_config


@pytest.fixture(autouse=True)
def _reset_state():
    reset_config()
    jwt_verify._jwks_clients.clear()
    yield
    reset_config()
    jwt_verify._jwks_clients.clear()
