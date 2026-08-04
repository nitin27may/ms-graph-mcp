"""Service-auth seam: disabled by default; MI stub is a hard error until built."""

from __future__ import annotations

import pytest

from ms_graph_mcp.entra.service_auth import ManagedIdentityVerifier, ServiceAuthVerifier


def test_managed_identity_verifier_not_implemented():
    with pytest.raises(NotImplementedError):
        ManagedIdentityVerifier()


def test_custom_verifier_satisfies_protocol():
    class _Allow:
        async def verify(self, request):
            return True

    assert isinstance(_Allow(), ServiceAuthVerifier)
