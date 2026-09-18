"""Client credentials beyond a secret, and refusing to start without one.

Microsoft's Agent ID guidance is explicit: client secrets "shouldn't be used as
client credentials in production environments" — use a certificate or a
federated identity credential. All three go through MSAL, so this adds no
dependency; what it adds is the precedence, and a startup check so a deployment
that cannot perform its own exchange says so at boot rather than on a user's
first tool call.
"""

from __future__ import annotations

import logging

import pytest

from ms_graph_mcp import obo
from ms_graph_mcp.app import build_app
from ms_graph_mcp.config import GraphMcpConfig

PEM = "-----BEGIN PRIVATE KEY-----\nMIIB...\n-----END PRIVATE KEY-----\n"


class TestCredentialPrecedence:
    """Certificate → federated → secret, and nothing when none is configured."""

    def test_nothing_configured_is_reported_plainly(self):
        kind, credential = obo._credential()
        assert kind == ""
        assert credential is None

    def test_a_secret_is_passed_through_as_a_string(self):
        kind, credential = obo._credential(client_secret="s3cret")
        assert (kind, credential) == ("secret", "s3cret")

    def test_a_certificate_wins_over_a_secret(self, tmp_path):
        pem_file = tmp_path / "cert.pem"
        pem_file.write_text(PEM)
        kind, credential = obo._credential(client_secret="s3cret", cert_path=str(pem_file))
        assert kind == "certificate"
        assert credential["private_key"] == PEM
        # Passing the bundle as public_certificate too is what lets MSAL derive
        # an SHA-256 thumbprint instead of the operator pasting one in.
        assert credential["public_certificate"] == PEM
        assert "passphrase" not in credential

    def test_an_encrypted_key_carries_its_passphrase(self, tmp_path):
        pem_file = tmp_path / "cert.pem"
        pem_file.write_text(PEM)
        _, credential = obo._credential(cert_path=str(pem_file), cert_passphrase="hunter2")
        assert credential["passphrase"] == "hunter2"

    def test_a_federated_token_wins_over_a_secret(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("assertion-jwt\n")
        kind, credential = obo._credential(
            client_secret="s3cret", federated_token_file=str(token_file)
        )
        assert kind == "federated"
        assert callable(credential["client_assertion"])

    def test_the_federated_assertion_is_read_on_demand(self, tmp_path):
        """The projected token is rotated — AKS refreshes it roughly hourly.

        Reading it once at startup works, and then silently stops working, which
        is the worst shape a credential bug can take. MSAL calls the callable
        when it needs to go on the wire, so a rotation is picked up.
        """
        token_file = tmp_path / "token"
        token_file.write_text("first\n")
        _, credential = obo._credential(federated_token_file=str(token_file))
        assertion = credential["client_assertion"]

        assert assertion() == "first"
        token_file.write_text("second\n")
        assert assertion() == "second"


class TestTheExchangeAcceptsEachCredential:
    async def test_a_certificate_needs_no_secret(self, tmp_path, monkeypatch):
        pem_file = tmp_path / "cert.pem"
        pem_file.write_text(PEM)
        captured: dict = {}

        class _App:
            def acquire_token_on_behalf_of(self, user_assertion, scopes):
                return {"access_token": "graph-tok"}

        def _fake_get_app(tenant_id, client_id, kind, credential):
            captured["kind"] = kind
            return _App()

        monkeypatch.setattr(obo, "_get_app", _fake_get_app)

        out = await obo.acquire_token_on_behalf_of(
            "user-tok",
            ["https://graph.microsoft.com/.default"],
            tenant_id="t",
            client_id="c",
            cert_path=str(pem_file),
        )
        assert out == "graph-tok"
        assert captured["kind"] == "certificate"

    async def test_no_credential_at_all_still_fails_closed(self):
        with pytest.raises(obo.OboError, match="not configured"):
            await obo.acquire_token_on_behalf_of("u", ["s"], tenant_id="t", client_id="c")

    def test_apps_are_cached_per_credential_kind(self, monkeypatch, tmp_path):
        """The cache key used to be (tenant, client) alone.

        With three credential kinds that would hand back an app built for the
        wrong one — silently, since MSAL only complains when it authenticates.
        """
        obo._apps.clear()
        built: list[tuple] = []

        class _FakeMsal:
            @staticmethod
            def ConfidentialClientApplication(**kwargs):  # noqa: N802 - MSAL's name
                built.append(("app", len(built)))
                return object()

        monkeypatch.setitem(__import__("sys").modules, "msal", _FakeMsal)

        first = obo._get_app("t", "c", "secret", "s")
        again = obo._get_app("t", "c", "secret", "s")
        other = obo._get_app("t", "c", "certificate", {"private_key": PEM})

        assert first is again
        assert other is not first
        assert len(built) == 2
        obo._apps.clear()


class TestStartupRefusesAnImpossibleDeployment:
    """`build_app()` only — a stdio session performs no exchange."""

    def _cfg(self, **overrides) -> GraphMcpConfig:
        base = {
            "_env_file": None,
            "mcp_does_obo": True,
            "tenant_id": "t",
            "client_id": "c",
            "client_secret": "s",
        }
        base.update(overrides)
        return GraphMcpConfig(**base)

    def test_a_configured_resource_server_starts(self):
        build_app(self._cfg())

    def test_no_credential_is_refused_at_boot(self):
        with pytest.raises(RuntimeError, match="GRAPH_MCP_CLIENT_CERT_PATH"):
            build_app(self._cfg(client_secret=""))

    def test_a_missing_tenant_is_named_specifically(self):
        with pytest.raises(RuntimeError, match="GRAPH_MCP_TENANT_ID"):
            build_app(self._cfg(tenant_id=""))

    def test_a_certificate_alone_is_enough(self, tmp_path):
        pem_file = tmp_path / "cert.pem"
        pem_file.write_text(PEM)
        build_app(self._cfg(client_secret="", client_cert_path=str(pem_file)))

    def test_a_federated_token_alone_is_enough(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("assertion")
        build_app(self._cfg(client_secret="", federated_token_file=str(token_file)))

    def test_the_passthrough_posture_needs_no_credential(self):
        """It never exchanges anything — the caller already did."""
        build_app(self._cfg(mcp_does_obo=False, client_secret="", tenant_id="", client_id=""))

    def test_a_secret_warns_but_starts(self, caplog):
        with caplog.at_level(logging.WARNING, logger="ms_graph_mcp.app"):
            build_app(self._cfg())
        assert "certificate" in caplog.text

    def test_a_certificate_does_not_warn(self, tmp_path, caplog):
        pem_file = tmp_path / "cert.pem"
        pem_file.write_text(PEM)
        with caplog.at_level(logging.WARNING, logger="ms_graph_mcp.app"):
            build_app(self._cfg(client_secret="", client_cert_path=str(pem_file)))
        assert "client secret" not in caplog.text
