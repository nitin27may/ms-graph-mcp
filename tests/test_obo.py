"""OBO exchange + OBO-mode dispatch for graph-mcp (resource-server posture, D4)."""

from __future__ import annotations

import json

import pytest

from ms_graph_mcp import obo, server
from ms_graph_mcp.config import GraphMcpConfig, set_config
from ms_graph_mcp.context import current_request_context


class _FakeApp:
    """Stand-in for msal.ConfidentialClientApplication."""

    def __init__(self, result: dict) -> None:
        self._result = result
        self.calls: list[tuple] = []

    def acquire_token_on_behalf_of(self, user_assertion, scopes):
        self.calls.append((user_assertion, tuple(scopes)))
        return self._result


# ── obo.acquire_token_on_behalf_of ────────────────────────────────────────────


async def test_obo_returns_access_token(monkeypatch):
    fake = _FakeApp({"access_token": "graph-tok", "expires_in": 3600})
    monkeypatch.setattr(obo, "_get_app", lambda *a, **k: fake)

    out = await obo.acquire_token_on_behalf_of(
        "user-tok",
        ["https://graph.microsoft.com/.default"],
        tenant_id="t",
        client_id="c",
        client_secret="s",
    )
    assert out == "graph-tok"
    assert fake.calls == [("user-tok", ("https://graph.microsoft.com/.default",))]


async def test_obo_raises_oboerror_on_aad_rejection(monkeypatch):
    fake = _FakeApp({"error": "invalid_grant", "error_description": "AADSTS50013 ..."})
    monkeypatch.setattr(obo, "_get_app", lambda *a, **k: fake)

    with pytest.raises(obo.OboError, match="invalid_grant"):
        await obo.acquire_token_on_behalf_of(
            "u", ["s"], tenant_id="t", client_id="c", client_secret="x"
        )


async def test_obo_requires_credentials():
    with pytest.raises(obo.OboError, match="not configured"):
        await obo.acquire_token_on_behalf_of(
            "u", ["s"], tenant_id="", client_id="c", client_secret="x"
        )


async def test_obo_requires_scopes():
    with pytest.raises(obo.OboError, match="scopes"):
        await obo.acquire_token_on_behalf_of(
            "u", [], tenant_id="t", client_id="c", client_secret="x"
        )


# ── dispatch in OBO mode ──────────────────────────────────────────────────────


def _obo_config() -> GraphMcpConfig:
    return GraphMcpConfig(
        _env_file=None,
        mcp_does_obo=True,
        tenant_id="t",
        client_id="c",
        client_secret="s",
    )


async def test_dispatch_obo_mode_exchanges_user_token_for_graph_token(monkeypatch):
    set_config(_obo_config())
    captured: dict = {}

    class _Reg:
        async def call(self, name, arguments_json, context):
            captured["context"] = context
            return {"ok": True, "tool": name}

    monkeypatch.setattr("ms_graph_mcp.server.get_registry", lambda: _Reg())

    async def _fake_obo(user_token, scopes, **kwargs):
        captured["obo_user_token"] = user_token
        captured["obo_scopes"] = list(scopes)
        return "graph-obo-token"

    monkeypatch.setattr("ms_graph_mcp.obo.acquire_token_on_behalf_of", _fake_obo)

    cv = current_request_context.set({"access_token": "user-tok", "user_email": "u@x.com"})
    try:
        await server.dispatch_graph_tool("get_my_profile", {})
    finally:
        current_request_context.reset(cv)

    # The tool ran with the OBO'd Graph token, not the inbound user token.
    assert captured["context"]["access_token"] == "graph-obo-token"
    assert captured["obo_user_token"] == "user-tok"
    assert captured["obo_scopes"] == ["https://graph.microsoft.com/.default"]


async def test_dispatch_obo_failure_returns_structured_error(monkeypatch):
    set_config(_obo_config())

    async def _boom(*a, **k):
        raise obo.OboError("OBO exchange failed (invalid_grant): nope")

    monkeypatch.setattr("ms_graph_mcp.obo.acquire_token_on_behalf_of", _boom)

    cv = current_request_context.set({"access_token": "user-tok", "user_email": "u@x.com"})
    try:
        result = await server.dispatch_graph_tool("get_my_profile", {})
    finally:
        current_request_context.reset(cv)

    payload = json.loads(result[0].text)
    assert payload["error"] == "obo_failed"


async def test_dispatch_obo_mode_still_fails_closed_without_token(monkeypatch):
    set_config(_obo_config())
    cv = current_request_context.set({"access_token": "", "user_email": ""})
    try:
        result = await server.dispatch_graph_tool("get_my_profile", {})
    finally:
        current_request_context.reset(cv)
    payload = json.loads(result[0].text)
    assert payload["error"] == "missing_graph_token"
