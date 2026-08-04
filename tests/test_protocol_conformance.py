"""End-to-end MCP protocol conformance, driven by the SDK's own client.

The rest of the suite calls the handlers directly, which proves the *logic* but
not that the server speaks the protocol — a handler can return a well-formed
Python object and still fail schema validation on the wire, or silently
negotiate down to a legacy protocol revision and drop fields nobody notices.

``mcp.Client`` accepts a ``Server`` instance and runs a real session against it
in-process: real ``initialize`` negotiation, real request/response validation,
no HTTP and no network. That makes this cheap enough to keep in the unit suite
while still catching what the direct-call tests cannot.
"""

from __future__ import annotations

import json

import mcp
import pytest

from ms_graph_mcp.allowlists import (
    INTERNAL_TOOL_NAME_SET,
    READ_TOOL_NAME_SET,
    WRITE_TOOL_NAME_SET,
)
from ms_graph_mcp.context import current_request_context
from ms_graph_mcp.server import SERVER_NAME, build_graph_mcp_server

# The revision this server is built against. If a future SDK negotiates lower by
# default, that is a regression we want to hear about.
EXPECTED_PROTOCOL_VERSION = "2026-07-28"


@pytest.fixture
def graph_client_context():
    """Set the request context for the duration of a client session.

    Restores by value rather than by token: the fixture body and the test
    coroutine run in different contexts, and ``ContextVar.reset()`` rejects a
    token minted in another one.
    """
    previous = current_request_context.get()

    def _set(**overrides):
        current_request_context.set({"access_token": "test-token", **overrides})

    yield _set
    current_request_context.set(previous)


async def test_negotiates_the_current_protocol_revision(graph_client_context):
    graph_client_context()
    async with mcp.Client(build_graph_mcp_server()) as client:
        assert client.protocol_version == EXPECTED_PROTOCOL_VERSION


async def test_server_info_reports_name_and_a_real_version(graph_client_context):
    """An empty version string in a client's server list costs diagnostic time."""
    graph_client_context()
    async with mcp.Client(build_graph_mcp_server()) as client:
        assert client.server_info.name == SERVER_NAME
        assert client.server_info.version
        assert client.server_info.version != "0.0.0+unknown"


async def test_list_tools_over_the_protocol_matches_the_read_allowlist(graph_client_context):
    graph_client_context()
    async with mcp.Client(build_graph_mcp_server()) as client:
        result = await client.list_tools()
    names = {tool.name for tool in result.tools}
    assert names == READ_TOOL_NAME_SET
    assert not names & WRITE_TOOL_NAME_SET
    assert not names & INTERNAL_TOOL_NAME_SET


async def test_list_tools_carries_cache_hints_to_a_modern_client(graph_client_context):
    """Cache hints only survive on the 2026-07-28 revision.

    A client that negotiates the legacy era has these stripped by the SDK, so
    this test doubles as a guard that we did not quietly regress the negotiated
    protocol version.
    """
    graph_client_context()
    async with mcp.Client(build_graph_mcp_server()) as client:
        result = await client.list_tools()
    assert result.ttl_ms == 300_000
    assert result.cache_scope == "public"


async def test_every_advertised_tool_has_a_protocol_valid_schema(graph_client_context):
    graph_client_context()
    async with mcp.Client(build_graph_mcp_server()) as client:
        result = await client.list_tools()
    for tool in result.tools:
        assert tool.description, f"{tool.name} has no description"
        assert tool.input_schema.get("type") == "object", tool.name
        assert "properties" in tool.input_schema, tool.name


async def test_unknown_tool_returns_a_tool_execution_error(graph_client_context):
    """Not a JSON-RPC error — the model should see it and self-correct."""
    graph_client_context()
    async with mcp.Client(build_graph_mcp_server()) as client:
        result = await client.call_tool("totally_made_up", {})
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == "tool_not_available"


async def test_write_tool_is_refused_over_the_protocol_without_scope(graph_client_context):
    graph_client_context(write_scope=False)
    async with mcp.Client(build_graph_mcp_server()) as client:
        result = await client.call_tool("send_email", {"to": "x@example.com"})
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == "write_scope_required"


async def test_write_tools_appear_over_the_protocol_with_scope(graph_client_context):
    graph_client_context(write_scope=True)
    async with mcp.Client(build_graph_mcp_server()) as client:
        result = await client.list_tools()
    names = {tool.name for tool in result.tools}
    assert WRITE_TOOL_NAME_SET <= names
    assert not names & INTERNAL_TOOL_NAME_SET


async def test_missing_token_fails_closed_over_the_protocol(graph_client_context):
    graph_client_context(access_token="")
    async with mcp.Client(build_graph_mcp_server()) as client:
        result = await client.call_tool("get_my_profile", {})
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == "missing_graph_token"
