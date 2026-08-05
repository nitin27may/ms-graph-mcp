"""Shared fixtures for ms-graph-mcp tests."""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types
import pytest

from ms_graph_mcp.config import get_config, reset_config


@pytest.fixture(autouse=True)
def _reset_graph_config():
    """build_app() mutates the cached config singleton; reset between tests so
    a shared_secret set by one test never leaks into another.

    Also pins the toolset profile to ``all``. The shipped default is ``core``,
    which is right for a real deployment but would mean every tier test was
    quietly asserting profile filtering as well. Tests that care about profiles
    set their own config.
    """
    reset_config()
    get_config().toolsets = "all"
    yield
    reset_config()


# ── MCP handler invocation ────────────────────────────────────────────────────
# The SDK 2.x handlers take (ServerRequestContext, params) and return protocol
# result objects. Both graph-mcp handlers ignore the context — everything they
# need arrives through `current_request_context`, set by the transport — so the
# fixtures below pass None rather than building a real session just to discard
# it. If a handler ever starts reading `ctx`, these fail loudly with an
# AttributeError rather than silently testing the wrong thing.


@pytest.fixture
def list_tools():
    """Call the ``tools/list`` handler, returning the advertised tools."""

    async def _list_tools() -> list[types.Tool]:
        from ms_graph_mcp.server import list_graph_tools

        result = await list_graph_tools(None, None)  # type: ignore[arg-type]
        return result.tools

    return _list_tools


@pytest.fixture
def call_tool():
    """Call the ``tools/call`` handler, returning the raw ``CallToolResult``."""

    async def _call_tool(name: str, arguments: dict | None = None) -> types.CallToolResult:
        from ms_graph_mcp.server import dispatch_graph_tool

        return await dispatch_graph_tool(
            None,  # type: ignore[arg-type]
            types.CallToolRequestParams(name=name, arguments=arguments or {}),
        )

    return _call_tool


@pytest.fixture
def call_tool_payload(call_tool):
    """Call a tool and parse its single text content block as JSON."""

    async def _payload(name: str, arguments: dict | None = None) -> Any:
        result = await call_tool(name, arguments)
        return json.loads(result.content[0].text)

    return _payload
