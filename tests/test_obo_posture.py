"""The resource-server posture: audience binding, scopes, and step-up.

The default. An agent acting for a signed-in user presents a token audienced to
**this** server, and the middleware exchanges it for a Graph token before any
tool runs.

Three properties matter here, and each of them was absent or wrong before:

1. A **Graph-audienced** token is refused. Accepting one is the token-passthrough
   pattern the MCP authorization spec forbids — the token was issued for Graph,
   not for this server, and `azp` says who minted it rather than who it is for.
2. A caller's **delegated scopes** decide what it may do. Audience binding proves
   the token is for this server; it says nothing about what its bearer was
   granted, so without `scp` any correctly-audienced token reached everything.
3. A Conditional Access **claims challenge** reaches the client as a 401 it can
   act on. Buried in a 200 tool result, MFA step-up simply cannot complete.
"""

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
from ms_graph_mcp.config import GRAPH_AUDIENCE, GraphMcpConfig, set_config
from ms_graph_mcp.context import current_request_context

TENANT = "tenant-1"
CLIENT = "mcp-client-id"
SECRET = "graph-mcp-fleet-secret-long-enough-value"
READ_SCOPE = "access_as_user"
WRITE_SCOPE = "access_as_user.write"


def _mint(*, aud: str = f"api://{CLIENT}", scp: str = READ_SCOPE, **claims) -> str:
    now = int(time.time())
    payload = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": aud,
        "exp": now + 3600,
        "tid": TENANT,
        "azp": "agent-identity-id",
        "preferred_username": "alice@example.com",
        "scp": scp,
    }
    payload.update(claims)
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        .rstrip(b"=")
        .decode()
    )
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


def _config(**overrides) -> GraphMcpConfig:
    base = {
        "_env_file": None,
        "tenant_id": TENANT,
        "client_id": CLIENT,
        "client_secret": "s",
        "shared_secret": SECRET,
        "jwt_verify": False,
        "mcp_does_obo": True,
    }
    base.update(overrides)
    return GraphMcpConfig(**base)


def _app(cfg: GraphMcpConfig) -> Starlette:
    set_config(cfg)

    async def mcp(request):
        return JSONResponse({"context": current_request_context.get()})

    app = Starlette(routes=[Route("/mcp", mcp, methods=["POST"])])
    app.add_middleware(GraphMcpAuthMiddleware, config=cfg.to_auth_config())
    return app


@pytest.fixture
def exchanged(monkeypatch):
    """Record the exchange and return a distinguishable Graph token."""
    seen: dict = {}

    async def _fake(user_token, scopes, **kwargs):
        seen["user_token"] = user_token
        seen["scopes"] = list(scopes)
        seen["credential"] = kwargs.get("credential")
        return "graph-token-from-obo"

    monkeypatch.setattr("ms_graph_mcp.obo.acquire_token_on_behalf_of", _fake)
    return seen


class TestAudienceBinding:
    def test_an_mcp_audienced_token_is_exchanged(self, exchanged):
        with TestClient(_app(_config())) as client:
            resp = client.post("/mcp", headers={"Authorization": f"Bearer {_mint()}"})

        assert resp.status_code == 200
        ctx = resp.json()["context"]
        # The tool surface sees the exchanged Graph token, never the inbound one.
        assert ctx["access_token"] == "graph-token-from-obo"
        assert exchanged["scopes"] == ["https://graph.microsoft.com/.default"]

    def test_a_graph_audienced_token_is_refused(self, exchanged):
        """The passthrough token the spec forbids. It must not be accepted."""
        with TestClient(_app(_config())) as client:
            resp = client.post(
                "/mcp", headers={"Authorization": f"Bearer {_mint(aud=GRAPH_AUDIENCE)}"}
            )

        assert resp.status_code == 401
        assert "user_token" not in exchanged, "a foreign-audience token reached the exchange"

    def test_the_machine_principal_is_not_exchanged(self, exchanged):
        """It presents a shared secret, not a user assertion — nothing to redeem."""
        with TestClient(_app(_config())) as client:
            resp = client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {SECRET}",
                    "X-Internal-Scope": "true",
                    "X-OBO-Token": "supplied-downstream-token",
                },
            )

        assert resp.status_code == 200
        assert resp.json()["context"]["access_token"] == "supplied-downstream-token"
        assert "user_token" not in exchanged


class TestScopeGate:
    """The gate itself is exercised against real RS256 signatures in
    ``tests/entra/test_middleware_mcp.py`` — a configured scope gate forces
    verification on, so it cannot be tested with the unsigned tokens here.
    What belongs at this level is how the gate reaches the MCP's own config,
    and who is exempt from it."""

    def test_the_required_scope_reaches_the_auth_config(self):
        assert _config(required_scope=READ_SCOPE).to_auth_config().required_scopes_set == {
            READ_SCOPE
        }

    def test_the_machine_principal_bypasses_the_scope_gate(self, exchanged):
        """It has no `scp` at all; gating it on one would lock out the internal tier."""
        cfg = _config(required_scope=READ_SCOPE)
        with TestClient(_app(cfg)) as client:
            resp = client.post("/mcp", headers={"Authorization": f"Bearer {SECRET}"})

        assert resp.status_code == 200

    def test_a_configured_scope_gate_forces_signature_verification(self):
        """An authorization check over an unverified token is forgeable.

        A gate that can be forged is worse than no gate, because it reads as
        protection.
        """
        auth = _config(required_scope=READ_SCOPE, jwt_verify=False).to_auth_config()
        assert auth.verify_signature is True


class TestWriteScopeBinding:
    """`X-Write-Scope` is a header the caller sets for itself.

    On its own it is a preference. When the deployment names a write scope, the
    token decides and the header may only narrow.
    """

    def test_header_alone_is_not_enough_when_a_write_scope_is_configured(self, exchanged):
        cfg = _config(write_scope_name=WRITE_SCOPE)
        with TestClient(_app(cfg)) as client:
            resp = client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {_mint()}", "X-Write-Scope": "true"},
            )

        assert resp.status_code == 200
        assert resp.json()["context"]["write_scope"] is False, (
            "an agent that was never granted the write scope reached the write tier "
            "by setting a header on itself"
        )

    def test_the_token_scope_grants_it(self, exchanged):
        cfg = _config(write_scope_name=WRITE_SCOPE)
        token = _mint(scp=f"{READ_SCOPE} {WRITE_SCOPE}")
        with TestClient(_app(cfg)) as client:
            resp = client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {token}", "X-Write-Scope": "true"},
            )

        assert resp.json()["context"]["write_scope"] is True

    def test_the_header_can_still_narrow(self, exchanged):
        """Holding the scope is authority; the caller may decline to use it."""
        cfg = _config(write_scope_name=WRITE_SCOPE)
        token = _mint(scp=f"{READ_SCOPE} {WRITE_SCOPE}")
        with TestClient(_app(cfg)) as client:
            resp = client.post("/mcp", headers={"Authorization": f"Bearer {token}"})

        assert resp.json()["context"]["write_scope"] is False

    def test_unconfigured_write_scope_keeps_the_header_semantics(self, exchanged):
        """The pre-existing behaviour, which is why the setting defaults empty."""
        with TestClient(_app(_config())) as client:
            resp = client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {_mint()}", "X-Write-Scope": "true"},
            )

        assert resp.json()["context"]["write_scope"] is True


class TestClaimsChallengePropagation:
    """Conditional Access step-up has to reach the client as a 401.

    Microsoft's guidance for the middle tier is explicit: answer 401 with the
    claims challenge in `WWW-Authenticate`, and the client acquires a fresh token
    presenting it. This used to be a 200 carrying a tool error, so step-up could
    not complete in any tenant with a policy.
    """

    @pytest.fixture
    def challenged(self, monkeypatch):
        challenge = '{"access_token":{"polids":{"essential":true,"values":["policy-id"]}}}'

        async def _fake(*args, **kwargs):
            raise obo.OboError(
                "OBO exchange failed (interaction_required): AADSTS50079",
                error_code="interaction_required",
                claims=challenge,
                correlation_id="corr-9",
            )

        monkeypatch.setattr("ms_graph_mcp.obo.acquire_token_on_behalf_of", _fake)
        return challenge

    def test_it_becomes_a_401_with_the_challenge(self, challenged):
        with TestClient(_app(_config())) as client:
            resp = client.post("/mcp", headers={"Authorization": f"Bearer {_mint()}"})

        assert resp.status_code == 401
        header = resp.headers["WWW-Authenticate"]
        assert 'error="interaction_required"' in header

        encoded = header.split('claims="')[1].split('"')[0]
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        assert decoded == challenged

    def test_the_correlation_id_survives_for_support(self, challenged):
        with TestClient(_app(_config())) as client:
            resp = client.post("/mcp", headers={"Authorization": f"Bearer {_mint()}"})
        assert resp.json()["correlation_id"] == "corr-9"

    def test_a_configuration_fault_is_a_500_not_a_401(self, monkeypatch):
        """A 401 here would loop the client against a problem no token can fix."""

        async def _fake(*args, **kwargs):
            raise obo.OboError(
                "OBO exchange failed (invalid_client): bad secret",
                error_code="invalid_client",
            )

        monkeypatch.setattr("ms_graph_mcp.obo.acquire_token_on_behalf_of", _fake)

        with TestClient(_app(_config()), raise_server_exceptions=False) as client:
            resp = client.post("/mcp", headers={"Authorization": f"Bearer {_mint()}"})

        assert resp.status_code == 500
        assert resp.json()["error"] == "obo_failed"
