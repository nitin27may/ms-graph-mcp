"""ServiceAuthMiddleware in AGENT_EDGE mode: token verify + App-Role gate."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ms_graph_mcp.entra import AuthConfig, AuthMode, ServiceAuthMiddleware
from ms_graph_mcp.entra.context import current_user_email

TENANT = "tenant-123"
CLIENT = "client-abc"


def _cfg(**kw) -> AuthConfig:
    base = {"tenant_id": TENANT, "client_id": CLIENT, "jwt_verify": True}
    base.update(kw)
    return AuthConfig(**base)


def _app(cfg, mode=AuthMode.AGENT_EDGE, service_verifier=None) -> Starlette:
    async def health(request):
        return JSONResponse({"ok": True})

    async def root(request):
        return JSONResponse({"user": current_user_email.get()})

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/", root, methods=["POST"]),
        ]
    )
    app.add_middleware(
        ServiceAuthMiddleware, config=cfg, mode=mode, service_verifier=service_verifier
    )
    return app


def test_health_is_public():
    with TestClient(_app(_cfg())) as client:
        assert client.get("/health").status_code == 200


def test_missing_bearer_is_401():
    with TestClient(_app(_cfg())) as client:
        assert client.post("/").status_code == 401


def test_invalid_token_is_401(patched_jwks):
    with TestClient(_app(_cfg())) as client:
        resp = client.post("/", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


def test_auth_only_admits_any_valid_user(make_token, patched_jwks):
    with TestClient(_app(_cfg())) as client:
        resp = client.post("/", headers={"Authorization": f"Bearer {make_token()}"})
    assert resp.status_code == 200
    assert resp.json()["user"] == "alice@example.com"


def test_required_role_present_admits(make_token, patched_jwks):
    cfg = _cfg(required_roles="meeting-prep.user")
    with TestClient(_app(cfg)) as client:
        resp = client.post("/", headers={"Authorization": f"Bearer {make_token()}"})
    assert resp.status_code == 200


def test_required_role_absent_is_403(make_token, patched_jwks):
    cfg = _cfg(required_roles="devops.user")
    with TestClient(_app(cfg)) as client:
        resp = client.post("/", headers={"Authorization": f"Bearer {make_token()}"})
    assert resp.status_code == 403


def test_app_only_denied_by_default_is_403(make_token, patched_jwks):
    cfg = _cfg(required_roles="meeting-prep.user")
    tok = make_token(idtyp="app", remove=("preferred_username", "scp"))
    with TestClient(_app(cfg)) as client:
        resp = client.post("/", headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 403


def test_custom_service_verifier_can_reject(make_token, patched_jwks):
    class _Deny:
        async def verify(self, request):
            return False

    with TestClient(_app(_cfg(), service_verifier=_Deny())) as client:
        resp = client.post("/", headers={"Authorization": f"Bearer {make_token()}"})
    assert resp.status_code == 401
