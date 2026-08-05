"""Service auth (GraphMcpAuthMiddleware over ms_graph_mcp.entra, DOWNSTREAM).

These cover the **passthrough** posture, where the caller forwards an
already-OBO'd Graph token and the server validates the Graph audience plus
``azp``. It is no longer the default — see ``tests/test_obo_posture.py`` for the
resource-server posture that is — so the config below sets it explicitly rather
than relying on a default that has moved.

Tool calls present the Graph token in Authorization (validated + azp-checked);
no-user hydration calls present the shared secret (machine bypass). jwt_verify is
off here (the signature path is covered by tests/entra/test_jwt_verify.py) so these
focus on the middleware wiring: bypass, azp gate, and the request-context dict.
"""

from __future__ import annotations

import base64
import json
import time

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ms_graph_mcp.auth import GraphMcpAuthMiddleware
from ms_graph_mcp.config import GRAPH_AUDIENCE, GraphMcpConfig, set_config
from ms_graph_mcp.context import current_request_context

SECRET = "graph-mcp-fleet-secret-long-enough-value"
TENANT = "tenant-1"
CLIENT = "our-app-client-id"


def _graph_mcp_config() -> GraphMcpConfig:
    """Passthrough posture, stated explicitly — the default is now the other one."""
    return GraphMcpConfig(
        _env_file=None,
        shared_secret=SECRET,
        tenant_id=TENANT,
        client_id=CLIENT,
        jwt_verify=False,
        mcp_does_obo=False,
    )


def _cfg():
    return _graph_mcp_config().to_auth_config()


def _mint(**claims) -> str:
    now = int(time.time())
    payload = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": GRAPH_AUDIENCE,
        "exp": now + 3600,
        "tid": TENANT,
        "azp": CLIENT,
        "preferred_username": "alice@example.com",
        "scp": "User.Read",
    }
    payload.update(claims)
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        .rstrip(b"=")
        .decode()
    )
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


def _build_app() -> Starlette:
    # The middleware takes an AuthConfig for token validation but reads the
    # package config for the posture, so both have to say passthrough or the
    # request reaches an OBO exchange these tests are not about.
    set_config(_graph_mcp_config())

    async def health(request):
        return JSONResponse({"ok": True})

    async def mcp(request):
        return JSONResponse({"context": current_request_context.get()})

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/mcp", mcp, methods=["POST"]),
        ]
    )
    app.add_middleware(GraphMcpAuthMiddleware, config=_cfg())
    return app


def test_health_is_public():
    with TestClient(_build_app()) as client:
        assert client.get("/health").status_code == 200


def test_missing_authorization_rejected():
    with TestClient(_build_app()) as client:
        assert client.post("/mcp").status_code == 401


def test_machine_secret_bypass_carries_no_graph_token():
    with TestClient(_build_app()) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {SECRET}"})
    assert resp.status_code == 200
    ctx = resp.json()["context"]
    assert ctx["access_token"] == ""  # no-user call → no Graph token
    assert ctx["user_email"] == "agent"


def test_valid_obo_token_validated_and_stashed():
    token = _mint()
    with TestClient(_build_app()) as client:
        resp = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}", "X-Write-Scope": "true"},
        )
    assert resp.status_code == 200
    ctx = resp.json()["context"]
    assert ctx["access_token"] == token
    assert ctx["user_email"] == "alice@example.com"
    assert ctx["write_scope"] is True


def test_foreign_app_azp_rejected():
    with TestClient(_build_app()) as client:
        resp = client.post(
            "/mcp", headers={"Authorization": f"Bearer {_mint(azp='some-other-app')}"}
        )
    assert resp.status_code == 403


def test_entra_app_token_header_propagated():
    with TestClient(_build_app()) as client:
        resp = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {_mint()}",
                "X-Entra-App-Token": "entra-tok",
            },
        )
    assert resp.json()["context"]["entra_app_token"] == "entra-tok"


# ── Internal-tier gate (S2, agentic audit) ───────────────────────────────────
#
# internal_scope used to be gated on principal.is_app_only, which is True for
# BOTH the machine-secret bypass AND any real Entra client-credentials token.
# Any app registration in the tenant with an azp-allowlisted / correctly-
# audienced app-only token could self-supply X-Internal-Scope: true and reach
# the internal (deterministic) tier — arbitrary Graph passthrough, drive
# upload, etc. This boundary previously had zero test coverage.


def test_internal_scope_denied_for_delegated_user_token():
    """A normal user (delegated) token self-supplying X-Internal-Scope must
    never unlock the internal tier — internal_scope stays False."""
    token = _mint()  # preferred_username + scp set → delegated, not app-only
    with TestClient(_build_app()) as client:
        resp = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}", "X-Internal-Scope": "true"},
        )
    assert resp.status_code == 200
    assert resp.json()["context"]["internal_scope"] is False


def test_real_app_only_token_rejected_outright():
    """A REAL Entra client-credentials token (not the machine-secret bypass)
    must be rejected at the auth layer before it can reach internal_scope at
    all — allow_app_only defaults False for both MCP postures, and legitimate
    app-only Graph access in this fleet already goes through the machine-
    secret bypass, never a raw app-only JWT presented directly."""
    app_only_token = _mint(idtyp="app", preferred_username="", scp="")
    with TestClient(_build_app()) as client:
        resp = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {app_only_token}",
                "X-Internal-Scope": "true",
            },
        )
    assert resp.status_code == 403


def test_internal_scope_granted_only_for_machine_secret_bypass():
    """The one caller that should ever unlock the internal tier: the
    machine-secret bypass itself, with X-Internal-Scope: true."""
    with TestClient(_build_app()) as client:
        resp = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {SECRET}",
                "X-Internal-Scope": "true",
                "X-OBO-Token": "obo-tok",
            },
        )
    assert resp.status_code == 200
    ctx = resp.json()["context"]
    assert ctx["internal_scope"] is True
    assert ctx["access_token"] == "obo-tok"  # internal mode: X-OBO-Token wins


def test_internal_scope_false_without_header_even_for_machine_secret():
    """The machine-secret bypass alone is not enough — X-Internal-Scope must
    also be explicitly set, so ordinary no-user hydration calls (tools/list)
    don't accidentally land in the internal tier."""
    with TestClient(_build_app()) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {SECRET}"})
    assert resp.status_code == 200
    assert resp.json()["context"]["internal_scope"] is False
