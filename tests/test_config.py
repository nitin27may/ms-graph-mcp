"""Config seam tests: defaults, dual env aliases, singleton override."""

from __future__ import annotations

import pytest

from ms_graph_mcp.config import (
    GRAPH_AUDIENCE,
    GraphMcpConfig,
    get_config,
    reset_config,
    set_config,
)


@pytest.fixture(autouse=True)
def _clean_config():
    reset_config()
    yield
    reset_config()


def test_defaults_match_app():
    cfg = GraphMcpConfig(_env_file=None)
    assert cfg.disable_ssl_verify is False
    assert cfg.send_email_allowed_domains == ""
    assert cfg.browse_max_files == 500


def test_package_scoped_env_names(monkeypatch):
    monkeypatch.setenv("GRAPH_MCP_DISABLE_SSL_VERIFY", "true")
    monkeypatch.setenv("GRAPH_MCP_BROWSE_MAX_FILES", "7")
    monkeypatch.setenv("GRAPH_MCP_SEND_EMAIL_ALLOWED_DOMAINS", "contoso.com")
    cfg = GraphMcpConfig(_env_file=None)
    assert cfg.disable_ssl_verify is True
    assert cfg.browse_max_files == 7
    assert cfg.send_email_allowed_domains == "contoso.com"


def test_azure_ad_env_names_honoured(monkeypatch):
    # The three app-registration fields also accept the conventional AZURE_AD_*
    # names, so an existing Azure environment drives the server unchanged.
    monkeypatch.setenv("AZURE_AD_TENANT_ID", "tenant-guid")
    monkeypatch.setenv("AZURE_AD_CLIENT_ID", "client-guid")
    monkeypatch.setenv("AZURE_AD_CLIENT_SECRET", "shhh")
    cfg = GraphMcpConfig(_env_file=None)
    assert cfg.tenant_id == "tenant-guid"
    assert cfg.client_id == "client-guid"
    assert cfg.client_secret == "shhh"


def test_graph_mcp_names_win_over_azure_ad(monkeypatch):
    # GRAPH_MCP_* is canonical — it must take precedence over the alias.
    monkeypatch.setenv("AZURE_AD_TENANT_ID", "from-azure")
    monkeypatch.setenv("GRAPH_MCP_TENANT_ID", "from-graph-mcp")
    cfg = GraphMcpConfig(_env_file=None)
    assert cfg.tenant_id == "from-graph-mcp"


def test_unprefixed_env_names_are_ignored(monkeypatch):
    # Every setting is read under a GRAPH_MCP_ prefix. Bare names like
    # DISABLE_SSL_VERIFY are common enough that another tool in the same
    # environment may well set one, and it must not silently reconfigure this
    # server — least of all the TLS or shared-secret settings.
    monkeypatch.setenv("DISABLE_SSL_VERIFY", "true")
    monkeypatch.setenv("BROWSE_MAX_FILES", "4")
    monkeypatch.setenv("AGENT_SHARED_SECRET", "leaked")
    cfg = GraphMcpConfig(_env_file=None)
    assert cfg.disable_ssl_verify is False
    assert cfg.browse_max_files == 500
    assert cfg.shared_secret == ""


def test_get_config_is_cached_and_overridable():
    first = get_config()
    assert get_config() is first
    override = GraphMcpConfig(_env_file=None)
    override.browse_max_files = 9
    set_config(override)
    assert get_config() is override
    assert get_config().browse_max_files == 9


# ── Resource-server OBO posture (D4) ──────────────────────────────────────────


def test_interim_auth_config_validates_graph_audience_and_azp():
    # Default (mcp_does_obo off): accept the agent's OBO'd Graph token.
    cfg = GraphMcpConfig(_env_file=None, client_id="our-app")
    ac = cfg.to_auth_config()
    assert ac.audience == GRAPH_AUDIENCE
    assert ac.allowed_azp == "our-app"


def test_obo_auth_config_validates_own_audience_and_drops_azp():
    cfg = GraphMcpConfig(_env_file=None, mcp_does_obo=True, client_id="our-app")
    ac = cfg.to_auth_config()
    # No explicit audience → derived from client_id (the user token's audience
    # under the single registration).
    assert ac.audience == ""
    assert ac.audience_list == ["api://our-app", "our-app"]
    # azp gate retired in favour of RFC 8707 audience binding.
    assert ac.allowed_azp == ""


def test_obo_auth_config_honours_explicit_path_b_audience():
    cfg = GraphMcpConfig(
        _env_file=None,
        mcp_does_obo=True,
        client_id="our-app",
        obo_audience="api://ms-graph-mcp-api",
    )
    ac = cfg.to_auth_config()
    assert ac.audience_list == ["api://ms-graph-mcp-api"]


def test_obo_scopes_default_and_override():
    assert GraphMcpConfig(_env_file=None).obo_scopes_list == [
        "https://graph.microsoft.com/.default"
    ]
    cfg = GraphMcpConfig(
        _env_file=None,
        obo_scopes="https://graph.microsoft.com/Mail.Read, https://graph.microsoft.com/Files.Read.All",
    )
    assert cfg.obo_scopes_list == [
        "https://graph.microsoft.com/Mail.Read",
        "https://graph.microsoft.com/Files.Read.All",
    ]
