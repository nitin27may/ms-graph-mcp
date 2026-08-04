"""AuthConfig: dual-alias env, derived audience/issuer, and verify-force logic."""

from __future__ import annotations

from ms_graph_mcp.entra.config import AuthConfig


def test_secure_defaults(monkeypatch):
    monkeypatch.delenv("JWT_VERIFY_SIGNATURE", raising=False)
    monkeypatch.delenv("WG_AUTH_JWT_VERIFY", raising=False)
    c = AuthConfig()
    assert c.jwt_verify is True
    assert c.clock_skew_seconds == 60
    assert "/health" in c.public_paths_set
    assert "/.well-known/agent-card.json" in c.public_paths_set


def test_audience_derived_from_client_id():
    c = AuthConfig(client_id="abc", audience="")
    assert c.audience_list == ["api://abc", "abc"]


def test_audience_explicit_overrides_client_id():
    c = AuthConfig(client_id="abc", audience="https://graph.microsoft.com")
    assert c.audience_list == ["https://graph.microsoft.com"]


def test_required_roles_csv_parsing():
    c = AuthConfig(required_roles="a, b ,c")
    assert c.required_roles_set == {"a", "b", "c"}


def test_allowed_azp_csv_parsing():
    c = AuthConfig(allowed_azp="app1,app2")
    assert c.allowed_azp_set == {"app1", "app2"}


def test_verify_forced_on_when_roles_set():
    c = AuthConfig(jwt_verify=False, required_roles="meeting-prep.user")
    assert c.verify_signature is True


def test_verify_off_when_no_roles_and_disabled():
    c = AuthConfig(jwt_verify=False)
    assert c.verify_signature is False


def test_effective_issuers_v1_and_v2():
    c = AuthConfig(tenant_id="t1")
    issuers = c.effective_issuers
    assert "https://login.microsoftonline.com/t1/v2.0" in issuers
    assert "https://sts.windows.net/t1/" in issuers


def test_issuer_override():
    c = AuthConfig(tenant_id="t1", issuer="https://custom/issuer")
    assert c.effective_issuers == {"https://custom/issuer"}


def test_dual_alias_env(monkeypatch):
    monkeypatch.setenv("AZURE_AD_TENANT_ID", "envtenant")
    monkeypatch.setenv("WG_AUTH_REQUIRED_ROLES", "r1,r2")
    c = AuthConfig()
    assert c.tenant_id == "envtenant"
    assert c.required_roles_set == {"r1", "r2"}
