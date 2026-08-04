"""Shared-secret machine bypass — fleet machine calls skip JWT verify + role gate."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ms_graph_mcp.entra import AuthConfig, AuthMode, ServiceAuthMiddleware
from ms_graph_mcp.entra.context import current_user_email

TENANT = "tenant-123"
CLIENT = "client-abc"
SECRET = "fleet-secret-correct-horse-battery-staple"


def _app(cfg) -> Starlette:
    async def root(request):
        return JSONResponse({"user": current_user_email.get()})

    app = Starlette(routes=[Route("/", root, methods=["POST"])])
    app.add_middleware(ServiceAuthMiddleware, config=cfg, mode=AuthMode.AGENT_EDGE)
    return app


def _cfg(**kw) -> AuthConfig:
    base = {
        "tenant_id": TENANT,
        "client_id": CLIENT,
        "jwt_verify": True,
        "shared_secret": SECRET,
    }
    base.update(kw)
    return AuthConfig(**base)


def test_machine_secret_bypasses_role_gate():
    # A role gate is set that nobody satisfies — the machine call must still pass.
    cfg = _cfg(required_roles="role.nobody.has")
    with TestClient(_app(cfg)) as client:
        resp = client.post(
            "/",
            headers={
                "Authorization": f"Bearer {SECRET}",
                "X-User-Email": "svc@example.com",
            },
        )
    assert resp.status_code == 200
    assert resp.json()["user"] == "svc@example.com"


def test_machine_secret_defaults_email_to_agent():
    with TestClient(_app(_cfg())) as client:
        resp = client.post("/", headers={"Authorization": f"Bearer {SECRET}"})
    assert resp.status_code == 200
    assert resp.json()["user"] == "agent"


def test_wrong_secret_same_length_falls_through_to_jwt(patched_jwks):
    wrong = "x" * len(SECRET)
    assert len(wrong) == len(SECRET)
    with TestClient(_app(_cfg())) as client:
        resp = client.post("/", headers={"Authorization": f"Bearer {wrong}"})
    # Not the secret → JWT path → not a valid token → 401.
    assert resp.status_code == 401


def test_no_secret_configured_rejects_empty_bearer():
    cfg = _cfg(shared_secret="")
    with TestClient(_app(cfg)) as client:
        resp = client.post("/", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401


def test_real_user_token_still_works_when_secret_configured(make_token, patched_jwks):
    with TestClient(_app(_cfg())) as client:
        resp = client.post("/", headers={"Authorization": f"Bearer {make_token()}"})
    assert resp.status_code == 200
    assert resp.json()["user"] == "alice@example.com"


def test_secret_check_uses_constant_time_compare():
    # Regression guard: the secret comparison must be timing-safe, never ==.
    from ms_graph_mcp.entra import middleware

    with open(middleware.__file__, encoding="utf-8") as fh:
        src = fh.read()
    assert "hmac.compare_digest" in src
    assert "== cfg.shared_secret" not in src
    assert "token == " not in src
