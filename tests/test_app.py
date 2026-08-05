"""Smoke tests for the assembled graph-mcp Streamable-HTTP app."""

from __future__ import annotations

from starlette.testclient import TestClient

from ms_graph_mcp.allowlists import READ_TOOL_NAMES
from ms_graph_mcp.app import build_app
from ms_graph_mcp.config import GraphMcpConfig


def test_health_reports_service_metadata():
    with TestClient(build_app(GraphMcpConfig(shared_secret=""))) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "ms-graph-mcp"
    assert body["tools"] == len(READ_TOOL_NAMES)


def test_mcp_endpoint_requires_a_token_when_secret_configured():
    # An unauthenticated request is rejected before the MCP transport
    # runs (no Bearer → 401).
    app = build_app(GraphMcpConfig(shared_secret="s3cr3t"))
    with TestClient(app) as client:
        resp = client.post("/mcp")
    assert resp.status_code == 401


def test_mcp_endpoint_requires_a_token_even_standalone():
    # The contract is now Authorization=Bearer on every call (gateway-friendly):
    # there is no "open when no secret" mode — a missing token is a 401.
    app = build_app(GraphMcpConfig(shared_secret=""))
    with TestClient(app) as client:
        resp = client.post("/mcp")
    assert resp.status_code == 401
