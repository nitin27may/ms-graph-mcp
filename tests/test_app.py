"""Smoke tests for the assembled graph-mcp Streamable-HTTP app."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from ms_graph_mcp.allowlists import READ_TOOL_NAMES
from ms_graph_mcp.app import ConfigurationError, build_app
from ms_graph_mcp.config import GraphMcpConfig


def _config(**overrides) -> GraphMcpConfig:
    """A runnable HTTP deployment.

    The default posture is resource server, so a confidential-client credential
    is part of the minimum viable configuration — build_app() refuses to start
    without one rather than failing later on an unrelated-looking Graph call.
    """
    base = {
        "_env_file": None,
        "tenant_id": "tenant-1",
        "client_id": "mcp-client-id",
        "client_secret": "s3cret",
        "shared_secret": "",
    }
    return GraphMcpConfig(**{**base, **overrides})


def test_health_reports_service_metadata():
    with TestClient(build_app(_config())) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "ms-graph-mcp"
    assert body["tools"] == len(READ_TOOL_NAMES)


def test_mcp_endpoint_requires_a_token_when_secret_configured():
    # An unauthenticated request is rejected before the MCP transport
    # runs (no Bearer → 401).
    app = build_app(_config(shared_secret="s3cr3t"))
    with TestClient(app) as client:
        resp = client.post("/mcp")
    assert resp.status_code == 401


def test_mcp_endpoint_requires_a_token_even_standalone():
    # The contract is now Authorization=Bearer on every call (gateway-friendly):
    # there is no "open when no secret" mode — a missing token is a 401.
    app = build_app(_config())
    with TestClient(app) as client:
        resp = client.post("/mcp")
    assert resp.status_code == 401


class TestStartupValidation:
    """The HTTP transport fails at startup rather than at the first tool call.

    A missing credential otherwise surfaces mid-session as an `obo_failed` on
    whatever tool the model happened to reach for — a long way from the cause,
    and easy to misread as a Graph problem.
    """

    def test_no_credential_refuses_to_start(self):
        with pytest.raises(ConfigurationError, match="GRAPH_MCP_CLIENT_CERT_PATH"):
            build_app(_config(client_secret=""))

    def test_the_message_names_the_opt_out_too(self):
        with pytest.raises(ConfigurationError, match="GRAPH_MCP_DOES_OBO=false"):
            build_app(_config(client_secret=""))

    def test_missing_tenant_or_client_id_refuses_to_start(self):
        with pytest.raises(ConfigurationError, match="TENANT_ID"):
            build_app(_config(tenant_id=""))

    def test_passthrough_needs_no_credential(self):
        """The deprecated posture never exchanges anything, so it needs none."""
        app = build_app(_config(client_secret="", mcp_does_obo=False))
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200

    def test_passthrough_warns_about_the_posture(self, caplog):
        with caplog.at_level("WARNING"):
            build_app(_config(client_secret="", mcp_does_obo=False))
        assert "GRAPH_MCP_DOES_OBO=false" in caplog.text
        assert "deprecated" in caplog.text

    def test_a_client_secret_warns_but_starts(self, caplog):
        """Microsoft's guidance is not to use secrets in production. Saying so
        once at startup is cheap; refusing to start would be obstructive."""
        with caplog.at_level("WARNING"):
            build_app(_config())
        assert "client secret" in caplog.text

    def test_a_certificate_does_not_warn(self, tmp_path, caplog):
        pem = tmp_path / "cert.pem"
        pem.write_text(
            "-----BEGIN PRIVATE KEY-----\nk\n-----END PRIVATE KEY-----\n"
            "-----BEGIN CERTIFICATE-----\nc\n-----END CERTIFICATE-----\n"
        )
        with caplog.at_level("WARNING"):
            build_app(_config(client_secret="", client_cert_path=str(pem)))
        assert "client secret" not in caplog.text

    def test_an_unreadable_certificate_names_the_file(self, tmp_path):
        with pytest.raises(ConfigurationError, match="could not be loaded"):
            build_app(_config(client_secret="", client_cert_path=str(tmp_path / "absent.pem")))
