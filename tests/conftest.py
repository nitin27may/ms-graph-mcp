"""Shared fixtures for ms-graph-mcp tests."""

from __future__ import annotations

import json
import socket
from typing import Any

import mcp.types as types
import pytest

from ms_graph_mcp.config import get_config, reset_config


class EscapedToTheNetwork(RuntimeError):
    """A test tried to open a real connection."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if a test reaches the network, rather than quietly doing it.

    Every test here mocks Graph, so a real connection means a mock is not
    applied — and the failure mode is nasty: the call goes out to Graph with
    whatever token is lying around, the test fails with a puzzling 401 instead
    of naming the unpatched call, and the suite gets slow and flaky in CI.

    This is not hypothetical. The tests patch the *global* ``httpx.AsyncClient``
    rather than the name bound inside ``ms_graph_mcp.client``, which works only
    while the two are the same module object. Aliasing the import — as an httpx2
    migration would — silently sends live requests to Microsoft. That was found
    by spiking exactly that change (see issue #25).

    Loopback stays open: the Starlette ``TestClient`` and the HTTP transport
    tests are in-process and legitimately use it.
    """
    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost"):
            raise EscapedToTheNetwork(
                f"a test tried to connect to {host!r}. Tests must not reach the "
                "network — a mock is missing, or is patching a name the code "
                "under test no longer resolves to."
            )
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)


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
