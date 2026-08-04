"""MCP server wiring for graph-mcp.

Bridges ``integrations/graph`` ``@tool`` registry to the Model Context
Protocol. Read tools are always advertised; write tools are advertised and
callable only when the request carries ``X-Write-Scope: true``.

``tools/list`` returns read-only or read+write depending on context.
``tools/call`` dispatches through the shared ToolRegistry with a
request-scoped context, gating writes on the scope flag.
"""

from __future__ import annotations

import json
import logging

import mcp.types as types
from mcp.server.lowlevel import Server

from ms_graph_mcp.allowlists import (
    ALL_TOOL_NAME_SET,
    APP_ONLY_INTERNAL_TOOL_NAME_SET,
    INTERNAL_TOOL_NAME_SET,
    READ_TOOL_NAME_SET,
    WRITE_TOOL_NAME_SET,
    resolve_internal_tools,
    resolve_read_tools,
    resolve_write_tools,
)
from ms_graph_mcp.config import get_config
from ms_graph_mcp.context import current_request_context
from ms_graph_mcp.tooling import ToolSpec, get_registry

logger = logging.getLogger(__name__)

SERVER_NAME = "ms-graph-mcp"


def _to_mcp_tool(spec: ToolSpec) -> types.Tool:
    return types.Tool(
        name=spec.name,
        description=spec.description,
        inputSchema=spec.parameters,
    )


def _error_content(error: str, message: str) -> list[types.TextContent]:
    """An MCP text result carrying a structured error the caller can act on."""
    return [
        types.TextContent(
            type="text",
            text=json.dumps({"error": error, "message": message}),
        )
    ]


async def list_graph_tools() -> list[types.Tool]:
    """MCP ``tools/list`` handler.

    Returns read-only tools by default. When the request context has
    ``write_scope=True``, write tools are appended.
    """
    tools = [_to_mcp_tool(spec) for spec in resolve_read_tools()]
    ctx = current_request_context.get()
    if ctx.get("write_scope"):
        tools.extend(_to_mcp_tool(spec) for spec in resolve_write_tools())
    # Internal (deterministic) tier — advertised ONLY to trusted internal callers
    # (machine principal + X-Internal-Scope), never to agents/external clients.
    if ctx.get("internal_scope"):
        tools.extend(_to_mcp_tool(spec) for spec in resolve_internal_tools())
    return tools


async def dispatch_graph_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    """MCP ``tools/call`` handler.

    Fail-closed:
    - A non-allowlisted name is rejected immediately.
    - A write tool called without write scope is rejected.
    - A missing Graph token is refused rather than calling Graph unauthed.
    """
    if name not in ALL_TOOL_NAME_SET:
        return _error_content(
            "tool_not_available",
            f"Tool '{name}' is not exposed by graph-mcp.",
        )

    # Gate the internal (deterministic) tier — callable only by trusted internal
    # callers (machine principal + X-Internal-Scope). Agents/external clients
    # presenting a user token never have internal_scope, so this fails closed.
    if name in INTERNAL_TOOL_NAME_SET:
        ctx = current_request_context.get()
        if not ctx.get("internal_scope"):
            return _error_content(
                "internal_scope_required",
                f"Tool '{name}' is an internal tool. Only the host application's own "
                "callers (machine principal + X-Internal-Scope: true) may invoke it.",
            )

    # App-only internal tools (revalidation probes) run on a client-credentials
    # token — no user session. Mint it here and skip the user-token guard + OBO.
    if name in APP_ONLY_INTERNAL_TOOL_NAME_SET:
        cfg = get_config()
        from ms_graph_mcp.obo import OboError, acquire_token_for_client

        try:
            app_token = await acquire_token_for_client(
                cfg.obo_scopes_list,
                tenant_id=cfg.tenant_id,
                client_id=cfg.client_id,
                client_secret=cfg.client_secret,
            )
        except OboError as exc:
            return _error_content("app_only_token_failed", f"client-credentials failed: {exc}")
        context = {**current_request_context.get(), "access_token": app_token}
        result = await get_registry().call(name, json.dumps(arguments or {}), context)
        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    # Gate write tools on explicit scope grant
    if name in WRITE_TOOL_NAME_SET:
        ctx = current_request_context.get()
        if not ctx.get("write_scope"):
            return _error_content(
                "write_scope_required",
                f"Tool '{name}' is a write tool. Caller must supply "
                "X-Write-Scope: true header to enable writes.",
            )

    context = current_request_context.get()
    if not context.get("access_token"):
        return _error_content(
            "missing_graph_token",
            "No Graph access token was supplied (X-Graph-Token header); "
            "graph-mcp cannot call Microsoft Graph without it.",
        )

    # Resource-server OBO (D4): in OBO mode the inbound token is the *user* token
    # audienced to this MCP — exchange it for a Microsoft Graph token before the
    # tool runs. In the interim posture the agent already forwarded a Graph token,
    # so this is a no-op. The exchange comes AFTER the missing-token guard so an
    # absent token still fails closed.
    cfg = get_config()
    if cfg.mcp_does_obo:
        from ms_graph_mcp.obo import OboError, acquire_token_on_behalf_of

        try:
            graph_token = await acquire_token_on_behalf_of(
                context["access_token"],
                cfg.obo_scopes_list,
                tenant_id=cfg.tenant_id,
                client_id=cfg.client_id,
                client_secret=cfg.client_secret,
            )
        except OboError as exc:
            return _error_content("obo_failed", f"graph-mcp OBO exchange failed: {exc}")
        context = {**context, "access_token": graph_token}

    result = await get_registry().call(name, json.dumps(arguments or {}), context)
    return [types.TextContent(type="text", text=json.dumps(result, default=str))]


def build_graph_mcp_server() -> Server:
    """Construct the low-level MCP Server with the graph tool handlers registered.

    ``validate_input=False`` — the shared ToolRegistry already validates
    arguments against each tool's Pydantic model and returns LLM-friendly
    structured errors, so MCP-layer JSON Schema validation would be a
    redundant, less helpful second gate.
    """
    server: Server = Server(SERVER_NAME)
    server.list_tools()(list_graph_tools)
    server.call_tool(validate_input=False)(dispatch_graph_tool)
    read_count = len(READ_TOOL_NAME_SET)
    write_count = len(WRITE_TOOL_NAME_SET)
    logger.info(
        "graph-mcp server built with %d read + %d write tools (%d total)",
        read_count,
        write_count,
        read_count + write_count,
    )
    return server
