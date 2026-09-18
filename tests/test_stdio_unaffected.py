"""The stdio transport is not affected by HTTP-only settings.

Local stdio clients — VS Code, Claude Code, Claude Desktop — are the priority
surface. They sign the user in interactively and already hold a Graph token, so
none of the resource-server machinery applies to them. Nothing enforced that.

The specific hazard this file exists for: ``GRAPH_MCP_DOES_OBO`` used to be read
inside ``dispatch_graph_tool``, which both transports share. Setting it with
stdio therefore broke every tool call — the interactive token is *already* a
Graph token, and Entra refuses to redeem a token audienced to another app. stdio
was safe only because the default was off, which is not a guarantee, and the
default is due to flip (issue #29).

These tests pin the property rather than the implementation: whatever the HTTP
transport does with a setting, a stdio session must behave as if the setting did
not exist.
"""

from __future__ import annotations

import json

import pytest

from ms_graph_mcp.config import GraphMcpConfig, set_config
from ms_graph_mcp.context import current_request_context
from ms_graph_mcp.stdio import _build_context

# Every setting that only means something to the HTTP transport. A stdio session
# must produce byte-identical behaviour with each of them set.
HTTP_ONLY_SETTINGS = {
    "mcp_does_obo": True,
    "resource_url": "https://mcp.example.com",
    "shared_secret": "s3cret",
    "allowed_hosts": "mcp.example.com",
    "jwt_verify": False,
    "client_secret": "a-secret",
}


@pytest.fixture
def stdio_context():
    """Set a stdio request context and restore the previous value afterwards.

    Restored *by value*, not by token: a fixture body and an async test run in
    different contexts, and ``ContextVar.reset()`` rejects a token minted in
    another one.
    """
    previous = current_request_context.get()
    current_request_context.set(
        {
            "access_token": "interactive-graph-token",
            "user_email": "alice@example.com",
            "write_scope": False,
            "transport": "stdio",
        }
    )
    yield
    current_request_context.set(previous)


class TestTheContextIgnoresHttpSettings:
    """``_build_context()`` reads three env vars and the sign-in credentials.

    Nothing else may leak into it, or an operator setting an HTTP variable in a
    shared ``.env`` changes how their editor behaves.
    """

    def test_every_http_only_setting_leaves_the_context_unchanged(self, monkeypatch):
        monkeypatch.setenv("GRAPH_MCP_ACCESS_TOKEN", "supplied")
        set_config(GraphMcpConfig(_env_file=None))
        baseline = _build_context()

        for setting, value in HTTP_ONLY_SETTINGS.items():
            set_config(GraphMcpConfig(_env_file=None, **{setting: value}))
            assert _build_context() == baseline, f"{setting} changed the stdio context"

    def test_all_of_them_together_leave_the_context_unchanged(self, monkeypatch):
        monkeypatch.setenv("GRAPH_MCP_ACCESS_TOKEN", "supplied")
        set_config(GraphMcpConfig(_env_file=None))
        baseline = _build_context()

        set_config(GraphMcpConfig(_env_file=None, **HTTP_ONLY_SETTINGS))
        assert _build_context() == baseline

    def test_the_context_carries_no_http_only_keys(self, monkeypatch):
        """``internal_scope`` and ``entra_app_token`` are middleware-set keys.

        A stdio session has no middleware, so a tier that gates on one of them
        is unreachable — which is the point.
        """
        monkeypatch.setenv("GRAPH_MCP_ACCESS_TOKEN", "supplied")
        set_config(GraphMcpConfig(_env_file=None, **HTTP_ONLY_SETTINGS))
        context = _build_context()
        assert "internal_scope" not in context
        assert "entra_app_token" not in context
        assert context["transport"] == "stdio"


class TestDispatchNeverObosOverStdio:
    """The regression guard proper.

    ``GRAPH_MCP_DOES_OBO`` is a transport setting. Over stdio there must be no
    code path that reaches an OBO exchange, even when it is misconfigured on.
    """

    async def test_obo_mode_does_not_exchange_a_stdio_token(
        self, monkeypatch, stdio_context, call_tool_payload
    ):
        set_config(
            GraphMcpConfig(
                _env_file=None,
                mcp_does_obo=True,
                tenant_id="t",
                client_id="c",
                client_secret="s",
            )
        )
        captured: dict = {}

        class _Registry:
            def canonical_name(self, name):
                return name

            async def call(self, name, arguments_json, context):
                captured["access_token"] = context["access_token"]
                return {"ok": True}

        monkeypatch.setattr("ms_graph_mcp.server.get_registry", lambda: _Registry())

        async def _must_not_run(*args, **kwargs):
            raise AssertionError("stdio reached the OBO exchange")

        monkeypatch.setattr("ms_graph_mcp.obo.acquire_token_on_behalf_of", _must_not_run)

        payload = await call_tool_payload("people_get_my_profile", {})

        assert payload == {"ok": True}
        # The tool ran with the token the user signed in for, untouched.
        assert captured["access_token"] == "interactive-graph-token"

    async def test_obo_mode_does_not_break_a_stdio_tool_call(
        self, monkeypatch, stdio_context, call_tool
    ):
        """The user-visible symptom of the old bug: every tool call failed.

        Asserted separately from the call-path guard above because this is what
        a user would actually report, and it holds even if the exchange moves
        somewhere else again.
        """
        set_config(
            GraphMcpConfig(
                _env_file=None,
                mcp_does_obo=True,
                tenant_id="t",
                client_id="c",
                client_secret="s",
            )
        )

        class _Registry:
            def canonical_name(self, name):
                return name

            async def call(self, name, arguments_json, context):
                return {"displayName": "Alice"}

        monkeypatch.setattr("ms_graph_mcp.server.get_registry", lambda: _Registry())

        result = await call_tool("people_get_my_profile", {})

        assert result.is_error is False
        assert json.loads(result.content[0].text) == {"displayName": "Alice"}

    async def test_a_missing_token_still_fails_closed_over_stdio(
        self, monkeypatch, call_tool_payload
    ):
        """Nothing here relaxes the fail-closed guard — it just moves the OBO."""
        set_config(GraphMcpConfig(_env_file=None, mcp_does_obo=True))
        previous = current_request_context.get()
        current_request_context.set({"access_token": "", "transport": "stdio"})
        try:
            payload = await call_tool_payload("people_get_my_profile", {})
        finally:
            current_request_context.set(previous)

        assert payload["error"] == "missing_graph_token"
        # The remedy names env vars, not an HTTP header.
        assert "GRAPH_MCP_CLIENT_ID" in payload["message"]
