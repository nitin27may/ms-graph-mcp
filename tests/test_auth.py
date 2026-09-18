"""Service auth (GraphMcpAuthMiddleware over ms_graph_mcp.entra, DOWNSTREAM).

Most of this file exercises the **passthrough** posture, where the caller
presents a Graph token it already exchanged: validated + azp-checked, with the
shared secret taking the machine bypass. jwt_verify is off there (the signature
path is covered by tests/entra/test_jwt_verify.py) so those tests focus on the
middleware wiring: bypass, azp gate, and the request-context dict.

The class at the end covers the **default** posture, and needs a real signed
token: audience validation only runs on the verified path, so the central claim
— a Graph-audienced token is refused — cannot be asserted with an unsigned one.
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


def _cfg():
    """The passthrough posture, explicitly.

    These tests mint Graph-audienced tokens, which is what a caller presents
    when it has already done the exchange itself. That is no longer the default
    — see `TestTheDefaultPostureRejectsAGraphToken` below for the default — so
    it has to be asked for.
    """
    cfg = GraphMcpConfig(
        shared_secret=SECRET,
        tenant_id=TENANT,
        client_id=CLIENT,
        jwt_verify=False,
        mcp_does_obo=False,
    )
    # Publish it as the active config as well. `build_app()` does this in
    # production, and the middleware reads posture-dependent settings
    # (`mcp_does_obo`, `write_scope_name`) from there rather than from the
    # entra AuthConfig, which does not carry them. Building one without the
    # other lets the two disagree, which is a test artefact, not a real state.
    set_config(cfg)
    return cfg.to_auth_config()


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


async def _context_route(request):
    return JSONResponse({"context": current_request_context.get()})


def _build_app() -> Starlette:
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


class TestTheDefaultPostureRejectsAGraphToken:
    """The confused-deputy mitigation, asserted end to end.

    A token whose `aud` is `https://graph.microsoft.com` was issued **for
    Graph**, not for this server. The MCP authorization spec says a server must
    validate that a token was issued specifically for it and must not accept
    ones that were not; Microsoft says the same from the other side — do not
    send a token anywhere except its intended audience. The old default did
    exactly that and narrowed it with `azp`, which says who *minted* a token,
    not who it is for.
    """

    def _resource_server_app(self, **overrides):
        from tests.conftest import TOKEN_CLIENT, TOKEN_TENANT

        cfg = GraphMcpConfig(
            _env_file=None,
            tenant_id=TOKEN_TENANT,
            client_id=TOKEN_CLIENT,
            client_secret="s",
            **overrides,
        )
        assert cfg.mcp_does_obo is True, "this test is about the default posture"
        set_config(cfg)
        app = Starlette(routes=[Route("/mcp", _context_route, methods=["POST"])])
        app.add_middleware(GraphMcpAuthMiddleware, config=cfg.to_auth_config())
        return app

    def test_a_graph_audienced_token_is_refused(self, make_token, patched_jwks):
        token = make_token(aud=GRAPH_AUDIENCE)
        with TestClient(self._resource_server_app()) as client:
            resp = client.post("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_a_token_audienced_to_this_server_is_accepted(self, make_token, patched_jwks):
        from tests.conftest import TOKEN_CLIENT

        token = make_token(aud=f"api://{TOKEN_CLIENT}")
        with TestClient(self._resource_server_app()) as client:
            resp = client.post("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["context"]["user_email"] == "alice@example.com"

    def test_the_bare_client_id_audience_is_accepted_too(self, make_token, patched_jwks):
        """v1-style tokens carry the bare GUID rather than the api:// URI."""
        from tests.conftest import TOKEN_CLIENT

        token = make_token(aud=TOKEN_CLIENT)
        with TestClient(self._resource_server_app()) as client:
            resp = client.post("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    def test_any_azp_is_accepted_because_the_audience_is_the_gate(self, make_token, patched_jwks):
        """The azp allowlist is dropped here — audience binding replaces it.

        `GRAPH_MCP_ALLOWED_AZP` can put it back as defence in depth; that is
        covered in tests/test_agent_identity.py.
        """
        from tests.conftest import TOKEN_CLIENT

        token = make_token(aud=f"api://{TOKEN_CLIENT}", azp="some-agent-identity")
        with TestClient(self._resource_server_app()) as client:
            resp = client.post("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
