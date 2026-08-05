"""ServiceAuthMiddleware in DOWNSTREAM_SERVICE mode: verify OBO token + azp, no role gate."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ms_graph_mcp.entra import AuthConfig, AuthMode, ServiceAuthMiddleware
from ms_graph_mcp.entra.context import current_access_token

TENANT = "tenant-123"
CLIENT = "client-abc"
GRAPH_AUD = "https://graph.microsoft.com"


def _mcp_cfg(**kw) -> AuthConfig:
    base = {
        "tenant_id": TENANT,
        "audience": GRAPH_AUD,
        "allowed_azp": CLIENT,
        "jwt_verify": True,
    }
    base.update(kw)
    return AuthConfig(**base)


def _app(cfg) -> Starlette:
    async def health(request):
        return JSONResponse({"ok": True})

    async def mcp(request):
        return JSONResponse({"token_present": bool(current_access_token.get())})

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/mcp", mcp, methods=["POST"]),
        ]
    )
    app.add_middleware(ServiceAuthMiddleware, config=cfg, mode=AuthMode.DOWNSTREAM_SERVICE)
    return app


def test_accepts_our_app_obo_token(make_token, patched_jwks):
    tok = make_token(aud=GRAPH_AUD)
    with TestClient(_app(_mcp_cfg())) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 200
    assert resp.json()["token_present"] is True


def test_rejects_foreign_azp_403(make_token, patched_jwks):
    tok = make_token(aud=GRAPH_AUD, azp="some-other-app")
    with TestClient(_app(_mcp_cfg())) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 403


def test_downstream_skips_role_gate(make_token, patched_jwks):
    # A required role is configured but DOWNSTREAM mode must NOT enforce it.
    cfg = _mcp_cfg(required_roles="role.nobody.has")
    tok = make_token(aud=GRAPH_AUD, roles=[])
    with TestClient(_app(cfg)) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 200


def test_missing_bearer_is_401(make_token, patched_jwks):
    with TestClient(_app(_mcp_cfg())) as client:
        assert client.post("/mcp").status_code == 401


# ── App-only policy applies in DOWNSTREAM_SERVICE mode too (S2, agentic audit) ──
#
# authenticate_request used to gate the app-only-token rejection on
# `mode == AGENT_EDGE` only, so a DOWNSTREAM_SERVICE (MCP) never rejected a
# real Entra client-credentials token even when allow_app_only was False —
# only the role gate was AGENT_EDGE-only by design (see AuthMode's
# docstring); the app-only policy was meant to apply everywhere.


def test_real_app_only_token_rejected_when_not_allowed(make_token, patched_jwks):
    """A genuine client-credentials token (idtyp=app, no user identity) hits
    a DOWNSTREAM_SERVICE with the default allow_app_only=False and must be
    rejected — not silently accepted as if it were a delegated call."""
    tok = make_token(remove=("preferred_username", "scp"), aud=GRAPH_AUD, idtyp="app")
    with TestClient(_app(_mcp_cfg())) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 403


def test_real_app_only_token_accepted_when_explicitly_allowed(make_token, patched_jwks):
    """The policy is configurable per-service — allow_app_only=True is the
    escape hatch for a future automation agent that legitimately needs to
    call a downstream service with a client-credentials token."""
    cfg = _mcp_cfg(allow_app_only=True)
    tok = make_token(remove=("preferred_username", "scp"), aud=GRAPH_AUD, idtyp="app")
    with TestClient(_app(cfg)) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 200


# ── Delegated-scope gate ──────────────────────────────────────────────────────
#
# Audience binding proves a token was minted for this service. It says nothing
# about what its bearer was granted, so without a `scp` check any client able to
# obtain a correctly-audienced token reached the entire surface. These run
# against real RS256 signatures because a configured scope gate forces
# verification on — an authorization check over an unverified token is
# forgeable, and a forgeable gate is worse than none.


def test_missing_required_scope_is_403(make_token, patched_jwks):
    cfg = _mcp_cfg(required_scopes="access_as_user")
    tok = make_token(aud=GRAPH_AUD, scp="something.else")
    with TestClient(_app(cfg)) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 403


def test_present_required_scope_is_admitted(make_token, patched_jwks):
    cfg = _mcp_cfg(required_scopes="access_as_user")
    tok = make_token(aud=GRAPH_AUD, scp="access_as_user")
    with TestClient(_app(cfg)) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 200


def test_every_required_scope_must_be_present(make_token, patched_jwks):
    """ALL, not any. Distinct scopes name distinct capabilities, so admitting a
    caller holding one of them would hand it the rest for free."""
    cfg = _mcp_cfg(required_scopes="access_as_user,access_as_user.write")
    tok = make_token(aud=GRAPH_AUD, scp="access_as_user")
    with TestClient(_app(cfg)) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 403


def test_no_configured_scope_gate_admits_any_scope(make_token, patched_jwks):
    """Defaults to off, so enabling it is a deliberate act rather than a break."""
    tok = make_token(aud=GRAPH_AUD, scp="whatever")
    with TestClient(_app(_mcp_cfg())) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 200
