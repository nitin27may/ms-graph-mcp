"""graph-mcp stdio transport — for local MCP clients (VS Code, Claude Code, Claude Desktop).

stdio has no per-request HTTP headers, so the session's Graph credentials come
from the environment. There are two ways to supply them, and the first is what
almost everyone should use:

**Interactive sign-in (recommended).** Set ``GRAPH_MCP_CLIENT_ID`` and
``GRAPH_MCP_TENANT_ID``. On first use the server signs the user in through the
browser — normal Microsoft 365 SSO, including MFA and conditional access — and
caches the result, so later sessions start without prompting. No token ever
appears in a config file.

**A pre-acquired token.** Set ``GRAPH_MCP_ACCESS_TOKEN``. Useful in CI or when
another component already holds a delegated token. Graph tokens last about an
hour, so this is not a workable way to run the server day to day.

Run via the ``ms-graph-mcp`` console script.
"""

from __future__ import annotations

import os
import sys

import anyio
from mcp.server.stdio import stdio_server

from ms_graph_mcp.config import get_config
from ms_graph_mcp.context import current_request_context
from ms_graph_mcp.server import build_graph_mcp_server


def _build_context() -> dict:
    """Assemble the request context for the single stdio session."""
    cfg = get_config()
    context: dict = {
        "user_email": os.getenv("GRAPH_MCP_USER_EMAIL", ""),
        "write_scope": os.getenv("GRAPH_MCP_WRITE_SCOPE", "").lower() == "true",
    }

    static_token = os.getenv("GRAPH_MCP_ACCESS_TOKEN", "")
    if static_token:
        context["access_token"] = static_token
        return context

    if cfg.client_id:
        from ms_graph_mcp.interactive_auth import InteractiveTokenProvider

        provider = InteractiveTokenProvider(
            client_id=cfg.client_id,
            tenant_id=cfg.tenant_id,
            scopes=cfg.scopes_list,
        )
        # Dispatch calls this before every tool, so the token is refreshed as it
        # ages rather than going stale an hour into the session.
        context["token_provider"] = provider.get_token
        context["access_token"] = ""
        return context

    # Neither route configured. Say so on stderr — stdout is the protocol
    # channel — and start anyway, so the client connects and every tool call
    # returns a readable error instead of the server dying at launch.
    print(
        "\n[ms-graph-mcp] No credentials configured. Set GRAPH_MCP_CLIENT_ID (and normally "
        "GRAPH_MCP_TENANT_ID) to sign in interactively, or GRAPH_MCP_ACCESS_TOKEN to supply a "
        "token directly. See the README.\n",
        file=sys.stderr,
        flush=True,
    )
    context["access_token"] = ""
    return context


async def _amain() -> None:
    current_request_context.set(_build_context())
    server = build_graph_mcp_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """Console entry point (``ms-graph-mcp``) — run the stdio server."""
    anyio.run(_amain)
