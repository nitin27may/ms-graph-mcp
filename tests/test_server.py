"""Contract tests for the graph-mcp MCP server handlers."""

from __future__ import annotations

import json

import mcp.types as types

from ms_graph_mcp.allowlists import READ_TOOL_NAME_SET
from ms_graph_mcp.context import current_request_context
from ms_graph_mcp.server import (
    SERVER_NAME,
    build_graph_mcp_server,
    list_graph_tools,
)


async def test_list_graph_tools_advertises_the_allowlist(list_tools):
    tools = await list_tools()
    assert {tool.name for tool in tools} == READ_TOOL_NAME_SET
    for tool in tools:
        assert isinstance(tool, types.Tool)
        assert tool.description
        assert tool.input_schema.get("type") == "object"


async def test_list_graph_tools_returns_cache_hints():
    """A deterministic, cacheable tool list improves client prompt-cache hits."""
    result = await list_graph_tools(None, None)
    assert result.ttl_ms and result.ttl_ms > 0
    assert result.cache_scope == "public"


async def test_dispatch_rejects_unknown_tool(call_tool):
    result = await call_tool("totally_made_up", {})
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == "tool_not_available"


async def test_dispatch_rejects_write_tool_without_scope(call_tool):
    # mail_send is a write tool; without write_scope in context it is refused.
    cv = current_request_context.set(
        {"access_token": "tok", "user_email": "u@test.com", "write_scope": False}
    )
    try:
        result = await call_tool("mail_send", {"to": "x@example.com"})
    finally:
        current_request_context.reset(cv)
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == "write_scope_required"


async def test_dispatch_fails_closed_without_graph_token(call_tool):
    cv = current_request_context.set({"access_token": "", "user_email": ""})
    try:
        result = await call_tool("people_get_my_profile", {})
    finally:
        current_request_context.reset(cv)
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == "missing_graph_token"


async def test_dispatch_routes_to_registry_with_request_context(monkeypatch, call_tool):
    captured: dict = {}

    class _FakeRegistry:
        # Stands in for ToolRegistry, so it honours the same interface.

        def canonical_name(self, name):

            return name

        async def call(self, name, arguments_json, context):
            captured["name"] = name
            captured["arguments_json"] = arguments_json
            captured["context"] = context
            return {"ok": True, "tool": name}

    monkeypatch.setattr("ms_graph_mcp.server.get_registry", lambda: _FakeRegistry())

    ctx = {"access_token": "graph-token-abc", "user_email": "u@example.com"}
    cv = current_request_context.set(ctx)
    try:
        result = await call_tool("people_get_my_profile", {"foo": "bar"})
    finally:
        current_request_context.reset(cv)

    assert captured["name"] == "people_get_my_profile"
    assert json.loads(captured["arguments_json"]) == {"foo": "bar"}
    assert captured["context"] == ctx
    assert json.loads(result.content[0].text) == {"ok": True, "tool": "people_get_my_profile"}
    assert result.is_error is False


async def test_dispatch_marks_registry_argument_errors_as_tool_errors(monkeypatch, call_tool):
    """The registry returns a structured dict for bad arguments instead of raising.

    That has to surface as isError, or the client never feeds it back to the
    model and the self-correction the structured error exists for never happens.
    """

    class _FakeRegistry:
        # Stands in for ToolRegistry, so it honours the same interface.

        def canonical_name(self, name):

            return name

        async def call(self, name, arguments_json, context):
            return {"error": "invalid_arguments", "message": "nope"}

    monkeypatch.setattr("ms_graph_mcp.server.get_registry", lambda: _FakeRegistry())

    cv = current_request_context.set({"access_token": "tok"})
    try:
        result = await call_tool("people_get_my_profile", {})
    finally:
        current_request_context.reset(cv)

    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == "invalid_arguments"


def test_build_graph_mcp_server_registers_handlers():
    server = build_graph_mcp_server()
    assert server.name == SERVER_NAME
    # SDK 2.x takes handlers as constructor callbacks rather than decorator
    # registrations, so assert the wired handlers are reachable by method.
    assert server.get_request_handler("tools/list") is not None
    assert server.get_request_handler("tools/call") is not None


async def test_dispatch_resolves_a_token_provider_before_calling(monkeypatch, call_tool):
    """The stdio transport signs in rather than being handed a token.

    It supplies a callable instead, and dispatch must resolve it on every call —
    Graph access tokens expire after about an hour, so a token captured once at
    startup goes stale mid-session.
    """
    captured: dict = {}

    class _Reg:
        def canonical_name(self, name):
            return name

        async def call(self, name, arguments_json, context):
            captured["token"] = context["access_token"]
            return {"ok": True}

    monkeypatch.setattr("ms_graph_mcp.server.get_registry", lambda: _Reg())

    calls = {"n": 0}

    def _provider():
        calls["n"] += 1
        return f"token-{calls['n']}"

    cv = current_request_context.set({"access_token": "", "token_provider": _provider})
    try:
        await call_tool("people_get_my_profile", {})
        first = captured["token"]
        await call_tool("people_get_my_profile", {})
    finally:
        current_request_context.reset(cv)

    assert first == "token-1"
    assert captured["token"] == "token-2", "the provider must be consulted on every call"


async def test_dispatch_reports_a_sign_in_failure_as_a_tool_error(call_tool):
    """A failed sign-in must reach the model, not crash the protocol."""

    def _provider():
        raise RuntimeError("device flow timed out")

    cv = current_request_context.set({"access_token": "", "token_provider": _provider})
    try:
        result = await call_tool("people_get_my_profile", {})
    finally:
        current_request_context.reset(cv)

    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert payload["error"] == "sign_in_failed"
    assert "device flow timed out" in payload["message"]
