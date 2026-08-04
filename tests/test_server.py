"""Contract tests for the graph-mcp MCP server handlers."""

from __future__ import annotations

import json

import mcp.types as types

from ms_graph_mcp.allowlists import READ_TOOL_NAME_SET
from ms_graph_mcp.context import current_request_context
from ms_graph_mcp.server import (
    SERVER_NAME,
    build_graph_mcp_server,
    dispatch_graph_tool,
    list_graph_tools,
)


async def test_list_graph_tools_advertises_the_allowlist():
    tools = await list_graph_tools()
    assert {tool.name for tool in tools} == READ_TOOL_NAME_SET
    for tool in tools:
        assert isinstance(tool, types.Tool)
        assert tool.description
        assert tool.inputSchema.get("type") == "object"


async def test_dispatch_rejects_unknown_tool():
    result = await dispatch_graph_tool("totally_made_up", {})
    payload = json.loads(result[0].text)
    assert payload["error"] == "tool_not_available"


async def test_dispatch_rejects_write_tool_without_scope():
    # send_email is a write tool; without write_scope in context it is refused.
    cv = current_request_context.set(
        {"access_token": "tok", "user_email": "u@test.com", "write_scope": False}
    )
    try:
        result = await dispatch_graph_tool("send_email", {"to": "x@example.com"})
    finally:
        current_request_context.reset(cv)
    payload = json.loads(result[0].text)
    assert payload["error"] == "write_scope_required"


async def test_dispatch_fails_closed_without_graph_token():
    cv = current_request_context.set({"access_token": "", "user_email": ""})
    try:
        result = await dispatch_graph_tool("get_my_profile", {})
    finally:
        current_request_context.reset(cv)
    payload = json.loads(result[0].text)
    assert payload["error"] == "missing_graph_token"


async def test_dispatch_routes_to_registry_with_request_context(monkeypatch):
    captured: dict = {}

    class _FakeRegistry:
        async def call(self, name, arguments_json, context):
            captured["name"] = name
            captured["arguments_json"] = arguments_json
            captured["context"] = context
            return {"ok": True, "tool": name}

    monkeypatch.setattr("ms_graph_mcp.server.get_registry", lambda: _FakeRegistry())

    ctx = {"access_token": "graph-token-abc", "user_email": "u@example.com"}
    cv = current_request_context.set(ctx)
    try:
        result = await dispatch_graph_tool("get_my_profile", {"foo": "bar"})
    finally:
        current_request_context.reset(cv)

    assert captured["name"] == "get_my_profile"
    assert json.loads(captured["arguments_json"]) == {"foo": "bar"}
    assert captured["context"] == ctx
    assert json.loads(result[0].text) == {"ok": True, "tool": "get_my_profile"}


def test_build_graph_mcp_server_registers_handlers():
    server = build_graph_mcp_server()
    assert server.name == SERVER_NAME
    assert types.ListToolsRequest in server.request_handlers
    assert types.CallToolRequest in server.request_handlers
