"""The OBO exchange itself, and the client credentials it can run on.

The *posture* — which transport exchanges, and what a failed exchange looks like
to a client — is covered by ``tests/test_obo_posture.py``. This file is about
``obo.py`` in isolation: the MSAL call, the error shape it produces, and how a
configured credential is assembled.
"""

from __future__ import annotations

import pytest

from ms_graph_mcp import obo


class _FakeApp:
    """Stand-in for msal.ConfidentialClientApplication."""

    def __init__(self, result: dict) -> None:
        self._result = result
        self.calls: list[tuple] = []

    def acquire_token_on_behalf_of(self, user_assertion, scopes):
        self.calls.append((user_assertion, tuple(scopes)))
        return self._result

    def acquire_token_for_client(self, scopes):
        self.calls.append((None, tuple(scopes)))
        return self._result


# ── acquire_token_on_behalf_of ────────────────────────────────────────────────


async def test_obo_returns_access_token(monkeypatch):
    fake = _FakeApp({"access_token": "graph-tok", "expires_in": 3600})
    monkeypatch.setattr(obo, "_get_app", lambda *a, **k: fake)

    out = await obo.acquire_token_on_behalf_of(
        "user-tok",
        ["https://graph.microsoft.com/.default"],
        tenant_id="t",
        client_id="c",
        credential="s",
    )
    assert out == "graph-tok"
    assert fake.calls == [("user-tok", ("https://graph.microsoft.com/.default",))]


async def test_obo_raises_oboerror_on_aad_rejection(monkeypatch):
    fake = _FakeApp({"error": "invalid_client", "error_description": "AADSTS7000215 ..."})
    monkeypatch.setattr(obo, "_get_app", lambda *a, **k: fake)

    with pytest.raises(obo.OboError, match="invalid_client") as caught:
        await obo.acquire_token_on_behalf_of(
            "u", ["s"], tenant_id="t", client_id="c", credential="x"
        )
    assert caught.value.error_code == "invalid_client"
    assert caught.value.needs_user_interaction is False


async def test_obo_requires_credentials():
    with pytest.raises(obo.OboError, match="not configured"):
        await obo.acquire_token_on_behalf_of(
            "u", ["s"], tenant_id="", client_id="c", credential="x"
        )


async def test_obo_requires_scopes():
    with pytest.raises(obo.OboError, match="scopes"):
        await obo.acquire_token_on_behalf_of("u", [], tenant_id="t", client_id="c", credential="x")


class TestClaimsChallenge:
    """Conditional Access step-up has to survive the exchange intact.

    Entra returns the policy's claims challenge in the error body. Losing it
    means the client cannot satisfy MFA — it only learns it was refused, retries
    the same token, and fails identically.
    """

    async def test_claims_are_carried_on_the_error(self, monkeypatch):
        challenge = '{"access_token":{"polids":{"essential":true,"values":["conditional-policy"]}}}'
        fake = _FakeApp(
            {
                "error": "interaction_required",
                "error_description": "AADSTS50079: multifactor authentication required",
                "claims": challenge,
                "correlation_id": "corr-1",
            }
        )
        monkeypatch.setattr(obo, "_get_app", lambda *a, **k: fake)

        with pytest.raises(obo.OboError) as caught:
            await obo.acquire_token_on_behalf_of(
                "u", ["s"], tenant_id="t", client_id="c", credential="x"
            )

        assert caught.value.claims == challenge
        assert caught.value.correlation_id == "corr-1"
        assert caught.value.needs_user_interaction is True

    async def test_interaction_errors_count_even_without_claims(self, monkeypatch):
        """Consent prompts arrive as an error code with no challenge attached."""
        fake = _FakeApp({"error": "consent_required", "error_description": "AADSTS65001"})
        monkeypatch.setattr(obo, "_get_app", lambda *a, **k: fake)

        with pytest.raises(obo.OboError) as caught:
            await obo.acquire_token_on_behalf_of(
                "u", ["s"], tenant_id="t", client_id="c", credential="x"
            )
        assert caught.value.needs_user_interaction is True


# ── Client credentials ────────────────────────────────────────────────────────


class TestCredentialPrecedence:
    """Certificate → federated → secret, most secure first.

    Precedence matters more than it looks: a secret left in the environment from
    an earlier deployment must not quietly outrank a certificate somebody
    deliberately configured.
    """

    def test_nothing_configured_is_none(self):
        assert obo.build_client_credential() is None

    def test_secret_alone(self):
        assert obo.build_client_credential(client_secret="s") == "s"

    def test_certificate_outranks_secret(self, tmp_path):
        pem = tmp_path / "cert.pem"
        pem.write_text(
            "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n"
            "-----BEGIN CERTIFICATE-----\ncert\n-----END CERTIFICATE-----\n"
        )
        credential = obo.build_client_credential(client_secret="s", cert_path=str(pem))

        assert isinstance(credential, dict)
        assert "PRIVATE KEY" in credential["private_key"]
        assert "CERTIFICATE" in credential["public_certificate"]
        # No thumbprint: MSAL derives an SHA-256 one from the certificate, which
        # avoids asking for the SHA-1 value it has deprecated.
        assert "thumbprint" not in credential

    def test_federated_outranks_secret(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("projected-assertion\n")

        credential = obo.build_client_credential(
            client_secret="s", federated_token_file=str(token_file)
        )

        assert callable(credential["client_assertion"])
        assert credential["client_assertion"]() == "projected-assertion"

    def test_federated_assertion_is_re_read_each_time(self, tmp_path):
        """AKS rotates the projected token; a value read once would expire."""
        token_file = tmp_path / "token"
        token_file.write_text("first")
        credential = obo.build_client_credential(federated_token_file=str(token_file))

        assert credential["client_assertion"]() == "first"
        token_file.write_text("second")
        assert credential["client_assertion"]() == "second"

    def test_missing_federated_token_file_names_the_cause(self, tmp_path):
        with pytest.raises(obo.CredentialError, match="workload-identity"):
            obo.build_client_credential(federated_token_file=str(tmp_path / "absent"))

    def test_a_pem_without_a_certificate_is_rejected(self, tmp_path):
        pem = tmp_path / "key-only.pem"
        pem.write_text("-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n")
        with pytest.raises(obo.CredentialError, match="openssl pkcs12"):
            obo.build_client_credential(cert_path=str(pem))


def test_credential_kind_distinguishes_the_app_cache_key():
    """The MSAL app is cached per credential kind, so the three cannot collide."""
    assert obo._credential_kind("secret") == "secret"
    assert obo._credential_kind({"client_assertion": lambda: "x"}) == "federated"
    assert obo._credential_kind({"private_key": "k"}) == "certificate"
