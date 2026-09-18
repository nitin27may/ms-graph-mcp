"""Write authority comes from the token, and a refusal is a spec-native challenge.

``X-Write-Scope: true`` is a header the caller sets for itself, so on its own it
is not authority — anyone who can reach the server can send it. Two changes,
which are the same change seen from each end (issues #29 §2 and #26):

- Once the inbound token is audienced to this MCP, ``scp`` says what the user
  actually consented to. The rule becomes ``header AND scope``: the header can
  only *narrow*, letting a cautious client decline write access it was granted.
- A write tool refused for want of a scope answers ``403`` with
  ``WWW-Authenticate: Bearer error="insufficient_scope", scope="…"``, which a
  conforming client steps up on without knowing anything about this server. A
  custom header only ever worked for a client that had been told about it.

The passthrough posture is untouched: there the token is audienced to Graph and
its ``scp`` carries Graph permissions, so the header still decides alone. That
is deprecated, not broken.
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

from ms_graph_mcp.auth import GraphMcpAuthMiddleware
from ms_graph_mcp.config import GraphMcpConfig, set_config
from ms_graph_mcp.context import current_request_context

TENANT = "tenant-1"
SECRET = "graph-mcp-fleet-secret-long-enough-value"
WRITE_TOOL = "mail_send"
READ_TOOL = "people_get_my_profile"


def _cfg(**overrides) -> GraphMcpConfig:
    """Resource-server posture — the one where `scp` means something."""
    base = {
        "_env_file": None,
        "mcp_does_obo": True,
        "tenant_id": TENANT,
        "client_id": "c",
        "client_secret": "s",
        "shared_secret": SECRET,
        "jwt_verify": False,
    }
    base.update(overrides)
    return GraphMcpConfig(**base)


def _mint(scp: str = "access_as_user") -> str:
    payload = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": "api://c",
        "exp": int(time.time()) + 3600,
        "tid": TENANT,
        "azp": "c",
        "preferred_username": "alice@example.com",
        "scp": scp,
    }
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        .rstrip(b"=")
        .decode()
    )
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


@pytest.fixture(autouse=True)
def _stub_the_exchange(monkeypatch):
    """The resource-server posture exchanges the token on every tool call.

    That is covered in tests/test_obo.py; here it would just be a network call
    waiting to happen, so it is stubbed for the whole module.
    """

    async def _exchanged(user_token, scopes, **kwargs):
        return "graph-token"

    monkeypatch.setattr("ms_graph_mcp.obo.acquire_token_on_behalf_of", _exchanged)


def _app(cfg: GraphMcpConfig) -> Starlette:
    async def mcp(request):
        return JSONResponse({"context": current_request_context.get()})

    app = Starlette(routes=[Route("/mcp", mcp, methods=["POST"])])
    app.add_middleware(GraphMcpAuthMiddleware, config=cfg.to_auth_config())
    return app


def _call(client, tool: str, *, token: str, write_header: bool = True):
    headers = {"Authorization": f"Bearer {token}"}
    if write_header:
        headers["X-Write-Scope"] = "true"
    return client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool}},
    )


class TestTheHeaderCanOnlyNarrow:
    def test_header_and_scope_together_grant_write(self):
        cfg = _cfg()
        set_config(cfg)
        with TestClient(_app(cfg)) as client:
            resp = _call(client, READ_TOOL, token=_mint("access_as_user access_as_user.write"))
        assert resp.status_code == 200
        assert resp.json()["context"]["write_scope"] is True

    def test_the_scope_alone_does_not_grant_write(self):
        """A client that holds write authority may still decline to use it."""
        cfg = _cfg()
        set_config(cfg)
        with TestClient(_app(cfg)) as client:
            resp = _call(
                client,
                READ_TOOL,
                token=_mint("access_as_user access_as_user.write"),
                write_header=False,
            )
        assert resp.status_code == 200
        assert resp.json()["context"]["write_scope"] is False

    def test_the_header_alone_does_not_grant_write(self):
        """The header is caller-supplied. Without the scope it is just a request."""
        cfg = _cfg()
        set_config(cfg)
        with TestClient(_app(cfg)) as client:
            resp = _call(client, READ_TOOL, token=_mint("access_as_user"))
        assert resp.status_code == 200
        assert resp.json()["context"]["write_scope"] is False

    def test_a_configurable_scope_name_is_honoured(self):
        cfg = _cfg(write_scope_name="mcp.write")
        set_config(cfg)
        with TestClient(_app(cfg)) as client:
            resp = _call(client, READ_TOOL, token=_mint("access_as_user mcp.write"))
        assert resp.json()["context"]["write_scope"] is True

    def test_the_machine_bypass_still_opts_in_by_header(self):
        """A first-party caller gated by the shared secret carries no `scp`."""
        cfg = _cfg()
        set_config(cfg)
        with TestClient(_app(cfg)) as client:
            resp = _call(client, READ_TOOL, token=SECRET)
        assert resp.json()["context"]["write_scope"] is True

    def test_the_passthrough_posture_is_unchanged(self):
        """Deprecated, not broken: a Graph-audienced token has no `scp` of ours."""
        cfg = _cfg(mcp_does_obo=False)
        set_config(cfg)
        with TestClient(_app(cfg)) as client:
            resp = _call(client, READ_TOOL, token=_mint("User.Read"))
        assert resp.json()["context"]["write_scope"] is True


class TestTheInsufficientScopeChallenge:
    def test_a_write_tool_without_the_scope_is_403_with_a_challenge(self):
        cfg = _cfg()
        set_config(cfg)
        with TestClient(_app(cfg)) as client:
            resp = _call(client, WRITE_TOOL, token=_mint("access_as_user"))

        assert resp.status_code == 403
        challenge = resp.headers["WWW-Authenticate"]
        assert 'error="insufficient_scope"' in challenge
        # The scope named is the one this refusal needs, not the general
        # advertisement — otherwise the client asks for the wrong thing.
        # Fully qualified: `scp` carries bare names, but an authorization
        # request needs the form Entra recognises.
        assert 'scope="api://c/access_as_user.write"' in challenge
        assert "access_as_user.write" in resp.json()["error"]

    def test_a_write_tool_with_the_scope_passes_through(self):
        cfg = _cfg()
        set_config(cfg)
        with TestClient(_app(cfg)) as client:
            resp = _call(client, WRITE_TOOL, token=_mint("access_as_user.write"))
        assert resp.status_code == 200

    def test_a_read_tool_is_never_challenged(self):
        cfg = _cfg()
        set_config(cfg)
        with TestClient(_app(cfg)) as client:
            resp = _call(client, READ_TOOL, token=_mint("access_as_user"))
        assert resp.status_code == 200
        assert "WWW-Authenticate" not in resp.headers

    def test_a_read_only_deployment_does_not_challenge(self):
        """No scope can unlock a write tool there, so naming one would mislead.

        Dispatch answers `read_only_deployment`, which says the real reason.
        """
        cfg = _cfg(read_only=True)
        set_config(cfg)
        with TestClient(_app(cfg)) as client:
            resp = _call(client, WRITE_TOOL, token=_mint("access_as_user"))
        assert resp.status_code == 200
        assert "WWW-Authenticate" not in resp.headers

    def test_the_passthrough_posture_does_not_challenge(self):
        """There is no scope of ours to challenge for in a Graph-audienced token."""
        cfg = _cfg(mcp_does_obo=False)
        set_config(cfg)
        with TestClient(_app(cfg)) as client:
            resp = _call(client, WRITE_TOOL, token=_mint("User.Read"), write_header=False)
        assert resp.status_code == 200

    def test_a_non_tool_call_is_never_challenged(self):
        cfg = _cfg()
        set_config(cfg)
        with TestClient(_app(cfg)) as client:
            resp = client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {_mint('access_as_user')}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        assert resp.status_code == 200
