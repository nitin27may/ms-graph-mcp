"""Restricting which agent identities may call (`GRAPH_MCP_ALLOWED_AZP`).

Audience binding is the gate in the resource-server posture: a token audienced
to this MCP was issued for this MCP, whoever asked for it. That is the right
primary control, and it is why the old `azp` allowlist was dropped when the
posture flipped.

`azp` is still useful as a *second* control. An Entra Agent ID token carries the
agent identity's client id there, so an operator who knows exactly which agents
should reach this server can say so. Empty by default, because switching it on
without knowing the ids locks everyone out.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ms_graph_mcp.auth import GraphMcpAuthMiddleware
from ms_graph_mcp.config import GraphMcpConfig, set_config
from ms_graph_mcp.context import current_request_context
from tests.conftest import TOKEN_CLIENT, TOKEN_TENANT

AGENT = "agent-identity-client-id"


def _app(**overrides) -> Starlette:
    cfg = GraphMcpConfig(
        _env_file=None,
        tenant_id=TOKEN_TENANT,
        client_id=TOKEN_CLIENT,
        client_secret="s",
        **overrides,
    )
    set_config(cfg)

    async def mcp(request):
        return JSONResponse({"context": current_request_context.get()})

    app = Starlette(routes=[Route("/mcp", mcp, methods=["POST"])])
    app.add_middleware(GraphMcpAuthMiddleware, config=cfg.to_auth_config())
    return app


def _get(app, token: str):
    with TestClient(app) as client:
        return client.post("/mcp", headers={"Authorization": f"Bearer {token}"})


def test_any_agent_is_accepted_by_default(make_token, patched_jwks):
    """The audience already proves the token was minted for this server."""
    token = make_token(aud=f"api://{TOKEN_CLIENT}", azp=AGENT)
    assert _get(_app(), token).status_code == 200


def test_a_listed_agent_is_accepted(make_token, patched_jwks):
    token = make_token(aud=f"api://{TOKEN_CLIENT}", azp=AGENT)
    assert _get(_app(allowed_azp=AGENT), token).status_code == 200


def test_an_unlisted_agent_is_refused(make_token, patched_jwks):
    token = make_token(aud=f"api://{TOKEN_CLIENT}", azp="some-other-agent")
    assert _get(_app(allowed_azp=AGENT), token).status_code == 403


def test_several_agents_can_be_listed(make_token, patched_jwks):
    token = make_token(aud=f"api://{TOKEN_CLIENT}", azp=AGENT)
    app = _app(allowed_azp=f"another-agent,{AGENT}")
    assert _get(app, token).status_code == 200


def test_the_allowlist_does_not_replace_audience_validation(make_token, patched_jwks):
    """A Graph-audienced token is refused even when its `azp` is allowed.

    This is the pairing that mattered: the old posture accepted a token minted
    for Graph *because* its `azp` matched. Listing an agent must not reopen
    that — `azp` says who asked for a token, never who it is for.
    """
    token = make_token(aud="https://graph.microsoft.com", azp=AGENT)
    assert _get(_app(allowed_azp=AGENT), token).status_code == 401
