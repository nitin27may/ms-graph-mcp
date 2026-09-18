"""Smoke tests for the assembled graph-mcp Streamable-HTTP app."""

from __future__ import annotations

from starlette.testclient import TestClient

from ms_graph_mcp.allowlists import READ_TOOL_NAMES
from ms_graph_mcp.app import build_app
from ms_graph_mcp.config import GraphMcpConfig


def _cfg(**overrides) -> GraphMcpConfig:
    """A startable config.

    Resource-server is the default posture and refuses to start without a client
    credential, so every app built here needs one — including the ones that only
    care about `/health` or a 401.
    """
    base = {"_env_file": None, "tenant_id": "t", "client_id": "c", "client_secret": "s"}
    return GraphMcpConfig(**{**base, **overrides})


def test_health_reports_service_metadata():
    with TestClient(build_app(_cfg(shared_secret=""))) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "ms-graph-mcp"
    assert body["tools"] == len(READ_TOOL_NAMES)


def test_mcp_endpoint_requires_a_token_when_secret_configured():
    # An unauthenticated request is rejected before the MCP transport
    # runs (no Bearer → 401).
    app = build_app(_cfg(shared_secret="s3cr3t"))
    with TestClient(app) as client:
        resp = client.post("/mcp")
    assert resp.status_code == 401


def test_mcp_endpoint_requires_a_token_even_standalone():
    # The contract is now Authorization=Bearer on every call (gateway-friendly):
    # there is no "open when no secret" mode — a missing token is a 401.
    app = build_app(_cfg(shared_secret=""))
    with TestClient(app) as client:
        resp = client.post("/mcp")
    assert resp.status_code == 401


def test_the_transport_still_receives_the_body_the_middleware_read():
    """The auth middleware reads the JSON-RPC body to find the method name.

    Starlette replays a body consumed in ``BaseHTTPMiddleware``, but that is a
    framework behaviour this server now depends on — and the failure mode if it
    ever stops holding is a transport that hangs or sees an empty request, not
    an import error. Drive a real ``initialize`` through the assembled app so a
    regression shows up here rather than in production.
    """
    cfg = _cfg(
        shared_secret="s3cr3t",
        # TestClient sends Host: testserver, which the DNS-rebinding guard
        # rejects by default.
        allowed_hosts="testserver",
    )
    with TestClient(build_app(cfg)) as client:
        resp = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer s3cr3t",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2026-07-28",
                    "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "1"},
                },
            },
        )

    assert resp.status_code == 200
    assert "ms-graph-mcp" in resp.text
