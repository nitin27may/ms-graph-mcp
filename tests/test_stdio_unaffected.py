"""The stdio transport must never perform an on-behalf-of exchange.

This is the regression guard for the resource-server work. VS Code, Claude Code
and Claude Desktop all run this server over stdio, where the user has signed in
interactively and the context already holds a **Graph** token. An OBO exchange
on that token cannot succeed — Entra refuses to redeem a token audienced to
another app ("Applications can't redeem a token for a different app") — so if
one is ever attempted, every tool call on every local client fails.

The exchange used to live in ``dispatch_graph_tool``, which both transports
share, gated only on ``mcp_does_obo``. That made the coupling accidental:
stdio was safe because of a default rather than because of a boundary, and
flipping the default to make the HTTP transport spec-conformant would have
broken every local client at once.

The exchange now lives in the HTTP auth middleware, which stdio never runs. So
``GRAPH_MCP_DOES_OBO`` is an HTTP-transport setting with no meaning for stdio,
and these tests assert that from the outside: with the setting turned **on**,
a stdio session must still behave exactly as it did with it off.
"""

from __future__ import annotations

import json

from ms_graph_mcp import stdio
from ms_graph_mcp.config import GraphMcpConfig, set_config
from ms_graph_mcp.context import current_request_context


class _Registry:
    """Stands in for ToolRegistry, so no Graph call is attempted."""

    def __init__(self) -> None:
        self.contexts: list[dict] = []

    def canonical_name(self, name: str) -> str:
        return name

    async def call(self, name: str, arguments_json: str, context: dict):
        self.contexts.append(context)
        return {"ok": True, "tool": name}


def _obo_enabled_config() -> GraphMcpConfig:
    """Resource-server settings — the posture stdio must ignore."""
    return GraphMcpConfig(
        _env_file=None,
        mcp_does_obo=True,
        tenant_id="t",
        client_id="c",
        client_secret="s",
        toolsets="all",
    )


class TestNoOboOnStdio:
    async def test_dispatch_never_exchanges_a_stdio_token(self, monkeypatch, call_tool):
        """The whole point. GRAPH_MCP_DOES_OBO=true must be inert here.

        The stdio token is already a Graph token. Exchanging it fails at Entra,
        so an attempt is not a degraded path — it is a total outage for every
        local client.
        """
        set_config(_obo_enabled_config())
        registry = _Registry()
        monkeypatch.setattr("ms_graph_mcp.server.get_registry", lambda: registry)

        async def _must_not_run(*args, **kwargs):
            raise AssertionError(
                "stdio attempted an OBO exchange. The exchange belongs to the HTTP "
                "middleware; dispatch must not reach it for any transport."
            )

        monkeypatch.setattr("ms_graph_mcp.obo.acquire_token_on_behalf_of", _must_not_run)

        cv = current_request_context.set(
            {"access_token": "interactive-graph-token", "transport": "stdio"}
        )
        try:
            result = await call_tool("people_get_my_profile", {})
        finally:
            current_request_context.reset(cv)

        assert result.is_error is False
        # The tool ran on the token the transport supplied, untouched.
        assert registry.contexts[0]["access_token"] == "interactive-graph-token"

    async def test_token_provider_is_still_resolved_every_call(self, monkeypatch, call_tool):
        """Interactive sign-in refreshes per call so a session cannot go stale.

        MSAL serves from its cache and only reaches the network once the token
        has actually aged out, so this is cheap — but it has to keep happening.
        """
        set_config(_obo_enabled_config())
        registry = _Registry()
        monkeypatch.setattr("ms_graph_mcp.server.get_registry", lambda: registry)

        issued: list[str] = []

        def _provider() -> str:
            issued.append(f"tok-{len(issued)}")
            return issued[-1]

        cv = current_request_context.set(
            {"access_token": "", "token_provider": _provider, "transport": "stdio"}
        )
        try:
            await call_tool("people_get_my_profile", {})
            await call_tool("people_get_my_profile", {})
        finally:
            current_request_context.reset(cv)

        assert issued == ["tok-0", "tok-1"]
        assert [c["access_token"] for c in registry.contexts] == ["tok-0", "tok-1"]

    async def test_missing_token_still_names_the_stdio_remedy(self, call_tool):
        """Fail closed, and point at an env var rather than an HTTP header."""
        set_config(_obo_enabled_config())
        cv = current_request_context.set({"access_token": "", "transport": "stdio"})
        try:
            result = await call_tool("people_get_my_profile", {})
        finally:
            current_request_context.reset(cv)

        payload = json.loads(result.content[0].text)
        assert payload["error"] == "missing_graph_token"
        assert "GRAPH_MCP_CLIENT_ID" in payload["message"]


class TestWriteScopeStaysEnvDriven:
    """`scp` binding is an HTTP-middleware concern. stdio has no principal."""

    async def test_env_write_scope_still_reaches_a_write_tool(self, monkeypatch, call_tool):
        set_config(_obo_enabled_config())
        registry = _Registry()
        monkeypatch.setattr("ms_graph_mcp.server.get_registry", lambda: registry)

        cv = current_request_context.set(
            {"access_token": "tok", "write_scope": True, "transport": "stdio"}
        )
        try:
            result = await call_tool("mail_mark_read", {"message_id": "abc", "is_read": True})
        finally:
            current_request_context.reset(cv)

        assert result.is_error is False, "stdio write tools must not require a scp claim"

    async def test_write_tools_still_refused_without_it(self, call_tool):
        set_config(_obo_enabled_config())
        cv = current_request_context.set(
            {"access_token": "tok", "write_scope": False, "transport": "stdio"}
        )
        try:
            result = await call_tool("mail_mark_read", {"message_id": "abc", "is_read": True})
        finally:
            current_request_context.reset(cv)

        payload = json.loads(result.content[0].text)
        assert payload["error"] == "write_scope_required"


class TestStdioStartsWithoutHttpCredentials:
    """The HTTP transport's fail-fast must not reach the stdio entry point.

    A workstation user has a client id and no secret. Requiring one to start
    would break every local install the moment the HTTP default changed.
    """

    def test_context_builds_with_no_client_secret(self, monkeypatch):
        monkeypatch.delenv("GRAPH_MCP_ACCESS_TOKEN", raising=False)
        monkeypatch.setenv("GRAPH_MCP_WRITE_SCOPE", "false")
        set_config(GraphMcpConfig(_env_file=None, mcp_does_obo=True, tenant_id="t", client_id="c"))

        context = stdio._build_context()

        assert context["transport"] == "stdio"
        assert "token_provider" in context, "interactive sign-in should still be wired"

    def test_context_builds_with_no_credentials_at_all(self, monkeypatch, capsys):
        """Still starts, so the client connects and every call explains itself."""
        monkeypatch.delenv("GRAPH_MCP_ACCESS_TOKEN", raising=False)
        set_config(GraphMcpConfig(_env_file=None, mcp_does_obo=True))

        context = stdio._build_context()

        assert context["access_token"] == ""
        assert "No credentials configured" in capsys.readouterr().err

    def test_a_static_token_is_passed_through_unchanged(self, monkeypatch):
        """The CI path. A pre-acquired Graph token must not be exchanged."""
        monkeypatch.setenv("GRAPH_MCP_ACCESS_TOKEN", "ci-graph-token")
        set_config(_obo_enabled_config())

        context = stdio._build_context()

        assert context["access_token"] == "ci-graph-token"
        assert "token_provider" not in context
