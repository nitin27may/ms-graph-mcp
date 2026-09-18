"""OBO exchange + OBO-mode dispatch for graph-mcp (resource-server posture, D4)."""

from __future__ import annotations

import base64
import json
import time

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ms_graph_mcp import obo
from ms_graph_mcp.auth import GraphMcpAuthMiddleware
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


# ── the exchange in the HTTP middleware ───────────────────────────────────────
#
# The exchange runs in the auth middleware rather than in dispatch, so that a
# Conditional Access claims challenge can come back as a 401 the client acts on
# — inside a tool result it would be sealed in an HTTP 200 and step-up could
# never complete. These tests drive it through the middleware for that reason;
# `tests/test_stdio_unaffected.py` guards the other half, that stdio can never
# reach it.

TENANT = "tenant-1"


def _obo_config(**overrides) -> GraphMcpConfig:
    return GraphMcpConfig(
        _env_file=None,
        mcp_does_obo=True,
        tenant_id=TENANT,
        client_id="c",
        client_secret="s",
        jwt_verify=False,
        **overrides,
    )


def _mint() -> str:
    """An unsigned user token — the signature path is covered in tests/entra/.

    Audience is not asserted here on purpose: with ``jwt_verify=False`` only
    ``exp`` and ``iss`` are checked, which keeps these tests about the exchange
    rather than about validation.
    """
    payload = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": "api://c",
        "exp": int(time.time()) + 3600,
        "tid": TENANT,
        "azp": "agent-app",
        "preferred_username": "alice@example.com",
        "scp": "access_as_user",
    }
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        .rstrip(b"=")
        .decode()
    )
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


def _app(cfg: GraphMcpConfig) -> Starlette:
    """A stand-in transport that reports the context the middleware built."""

    async def mcp(request):
        return JSONResponse({"context": current_request_context.get()})

    app = Starlette(routes=[Route("/mcp", mcp, methods=["POST"])])
    app.add_middleware(GraphMcpAuthMiddleware, config=cfg.to_auth_config())
    return app


def _call(client, tool: str = "people_get_my_profile", token: str | None = None):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token or _mint()}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool}},
    )


def test_middleware_exchanges_the_user_token_for_a_graph_token(monkeypatch):
    cfg = _obo_config()
    set_config(cfg)
    captured: dict = {}

    async def _fake_obo(user_token, scopes, **kwargs):
        captured["user_token"] = user_token
        captured["scopes"] = list(scopes)
        return "graph-obo-token"

    monkeypatch.setattr("ms_graph_mcp.obo.acquire_token_on_behalf_of", _fake_obo)

    token = _mint()
    with TestClient(_app(cfg)) as client:
        resp = _call(client, token=token)

    assert resp.status_code == 200
    # Downstream sees the exchanged Graph token, never the inbound assertion.
    assert resp.json()["context"]["access_token"] == "graph-obo-token"
    assert captured["user_token"] == token
    assert captured["scopes"] == ["https://graph.microsoft.com/.default"]


def test_a_claims_challenge_comes_back_as_a_401_the_client_can_act_on(monkeypatch):
    """Conditional Access step-up. The whole point of moving the exchange."""
    cfg = _obo_config()
    set_config(cfg)
    claims = '{"access_token":{"amr":{"values":["mfa"]}}}'

    async def _needs_mfa(*a, **k):
        raise obo.OboError(
            "OBO exchange failed (interaction_required): AADSTS50076",
            error_code="interaction_required",
            claims=claims,
            correlation_id="corr-1",
        )

    monkeypatch.setattr("ms_graph_mcp.obo.acquire_token_on_behalf_of", _needs_mfa)

    with TestClient(_app(cfg)) as client:
        resp = _call(client)

    assert resp.status_code == 401
    challenge = resp.headers["WWW-Authenticate"]
    assert 'error="interaction_required"' in challenge
    # The claims travel base64-encoded — raw JSON would not survive header parsing.
    encoded = base64.b64encode(claims.encode()).decode()
    assert f'claims="{encoded}"' in challenge


def test_an_unfixable_rejection_is_a_502_not_a_401(monkeypatch):
    """A 401 would send the client round a sign-in loop that cannot help."""
    cfg = _obo_config()
    set_config(cfg)

    async def _boom(*a, **k):
        raise obo.OboError("OBO exchange failed (invalid_grant): nope", error_code="invalid_grant")

    monkeypatch.setattr("ms_graph_mcp.obo.acquire_token_on_behalf_of", _boom)

    with TestClient(_app(cfg)) as client:
        resp = _call(client)

    assert resp.status_code == 502
    assert "invalid_grant" in resp.json()["error"]
    assert "WWW-Authenticate" not in resp.headers


def test_only_tool_calls_are_exchanged(monkeypatch):
    """`initialize` and `tools/list` need no Graph token.

    Exchanging on every request would add a round-trip to the handshake and let
    a Graph-side failure refuse a listing that does not touch Graph.
    """
    cfg = _obo_config()
    set_config(cfg)

    async def _must_not_run(*a, **k):
        raise AssertionError("exchanged a token for a non-tools/call request")

    monkeypatch.setattr("ms_graph_mcp.obo.acquire_token_on_behalf_of", _must_not_run)

    token = _mint()
    with TestClient(_app(cfg)) as client:
        resp = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert resp.status_code == 200
    assert resp.json()["context"]["access_token"] == token


def test_the_interim_posture_forwards_the_token_untouched(monkeypatch):
    """`mcp_does_obo=False` means the caller already did the exchange."""
    cfg = GraphMcpConfig(_env_file=None, mcp_does_obo=False, tenant_id=TENANT, jwt_verify=False)
    set_config(cfg)

    async def _must_not_run(*a, **k):
        raise AssertionError("passthrough posture reached the OBO exchange")

    monkeypatch.setattr("ms_graph_mcp.obo.acquire_token_on_behalf_of", _must_not_run)

    token = _mint()
    with TestClient(_app(cfg)) as client:
        resp = _call(client, token=token)

    assert resp.status_code == 200
    assert resp.json()["context"]["access_token"] == token


async def test_dispatch_still_fails_closed_without_token(call_tool_payload):
    """Moving the exchange must not relax the guard it used to sit behind."""
    set_config(_obo_config())
    previous = current_request_context.get()
    current_request_context.set({"access_token": "", "user_email": ""})
    try:
        payload = await call_tool_payload("people_get_my_profile", {})
    finally:
        current_request_context.set(previous)
    assert payload["error"] == "missing_graph_token"
