"""MCP server wiring for graph-mcp.

Bridges the ``ms_graph_mcp`` ``@tool`` registry to the Model Context
Protocol. Read tools are always advertised; write tools are advertised and
callable only when the request carries ``X-Write-Scope: true``.

``tools/list`` returns read-only or read+write depending on context.
``tools/call`` dispatches through the shared ToolRegistry with a
request-scoped context, gating writes on the scope flag.

MCP SDK 2.x note: handlers are passed to the ``Server`` constructor as
``on_list_tools`` / ``on_call_tool`` rather than registered by decorator, and
must return real ``ListToolsResult`` / ``CallToolResult`` objects — the SDK no
longer wraps a bare list, and validates results against the protocol schema.
"""

from __future__ import annotations

import json
import logging

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import ServerRequestContext

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


def _server_version() -> str:
    """The installed package version, reported in ``serverInfo``.

    Clients surface this in their server lists and bug reports, so an empty
    string here costs real diagnostic time later. Falls back gracefully when the
    package is being run from a source tree that was never installed.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("ms-graph-mcp")
    except PackageNotFoundError:  # pragma: no cover - only when not installed
        return "0.0.0+unknown"


# The tool surface only changes when the caller's scopes change, so a client may
# cache it. `public` is safe because the set is derived from the allowlists and
# the presented authorization, never from anything user-identifying.
_TOOLS_CACHE_TTL_MS = 300_000
_TOOLS_CACHE_SCOPE = "public"


def _to_mcp_tool(spec: ToolSpec) -> types.Tool:
    """Project a registry ToolSpec onto the wire type.

    Annotations are what a client uses to decide whether a call needs human
    confirmation. A tool that declares none gets MCP's most cautious defaults —
    potentially destructive, non-idempotent — so an unannotated read tool can
    make a client prompt the user before reading a calendar.
    """
    annotations = None
    if spec.annotations is not None:
        annotations = types.ToolAnnotations(
            readOnlyHint=spec.annotations.read_only,
            destructiveHint=spec.annotations.destructive,
            idempotentHint=spec.annotations.idempotent,
            openWorldHint=spec.annotations.open_world,
        )
    return types.Tool(
        name=spec.name,
        description=spec.description,
        inputSchema=spec.parameters,
        annotations=annotations,
    )


def _error_result(error: str, message: str) -> types.CallToolResult:
    """A tool-execution error the model can act on.

    ``isError=True`` is what tells the client to feed this back to the model for
    self-correction, rather than surfacing it as a protocol failure. The JSON
    body keeps the machine-readable ``error`` code callers already match on.
    """
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=json.dumps({"error": error, "message": message}),
            )
        ],
        isError=True,
    )


async def list_graph_tools(
    ctx: ServerRequestContext,
    params: types.PaginatedRequestParams | None = None,
) -> types.ListToolsResult:
    """MCP ``tools/list`` handler.

    Returns read-only tools by default. When the request context has
    ``write_scope=True``, write tools are appended.

    The 2026-07-28 spec permits the advertised set to vary with the
    authorization presented on the request, which is what makes the
    scope-dependent surface below conformant rather than a quirk.
    """
    tools = [_to_mcp_tool(spec) for spec in resolve_read_tools()]
    request_ctx = current_request_context.get()
    if request_ctx.get("write_scope"):
        tools.extend(_to_mcp_tool(spec) for spec in resolve_write_tools())
    # Internal (deterministic) tier — advertised ONLY to trusted internal
    # callers (machine principal + X-Internal-Scope), never to agents/external
    # clients.
    if request_ctx.get("internal_scope"):
        tools.extend(_to_mcp_tool(spec) for spec in resolve_internal_tools())
    return types.ListToolsResult(
        tools=tools,
        ttlMs=_TOOLS_CACHE_TTL_MS,
        cacheScope=_TOOLS_CACHE_SCOPE,
    )


async def dispatch_graph_tool(
    ctx: ServerRequestContext,
    params: types.CallToolRequestParams,
) -> types.CallToolResult:
    """MCP ``tools/call`` handler.

    Fail-closed:
    - A non-allowlisted name is rejected immediately.
    - A write tool called without write scope is rejected.
    - A missing Graph token is refused rather than calling Graph unauthed.
    """
    arguments = params.arguments

    # Resolve a superseded name to its canonical one before anything else. The
    # allowlists and every tier check below are written in canonical names only,
    # so an alias reaching them would be rejected as unknown. Advertised names
    # are always canonical — an alias only ever arrives from a caller that was
    # written against an older release.
    name = get_registry().canonical_name(params.name) or params.name

    if name not in ALL_TOOL_NAME_SET:
        return _error_result(
            "tool_not_available",
            f"Tool '{params.name}' is not exposed by graph-mcp.",
        )

    # Gate the internal (deterministic) tier — callable only by trusted internal
    # callers (machine principal + X-Internal-Scope). Agents/external clients
    # presenting a user token never have internal_scope, so this fails closed.
    if name in INTERNAL_TOOL_NAME_SET:
        request_ctx = current_request_context.get()
        if not request_ctx.get("internal_scope"):
            return _error_result(
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
            return _error_result("app_only_token_failed", f"client-credentials failed: {exc}")
        context = {**current_request_context.get(), "access_token": app_token}
        result = await get_registry().call(name, json.dumps(arguments or {}), context)
        return _success_result(result)

    # Gate write tools on explicit scope grant
    if name in WRITE_TOOL_NAME_SET:
        request_ctx = current_request_context.get()
        if not request_ctx.get("write_scope"):
            return _error_result(
                "write_scope_required",
                f"Tool '{name}' is a write tool. Caller must supply "
                "X-Write-Scope: true header to enable writes.",
            )

    context = current_request_context.get()
    if not context.get("access_token"):
        return _error_result(
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
            return _error_result("obo_failed", f"graph-mcp OBO exchange failed: {exc}")
        context = {**context, "access_token": graph_token}

    result = await get_registry().call(name, json.dumps(arguments or {}), context)
    return _success_result(result)


def _success_result(result: object) -> types.CallToolResult:
    """Wrap a tool's return value as an MCP result.

    The registry returns a structured ``{"error": "invalid_arguments", ...}`` dict
    for malformed arguments rather than raising, so that a model can correct
    itself. Surface those as ``isError`` too — otherwise the client has no signal
    that the call failed and the correction never happens.
    """
    is_error = isinstance(result, dict) and "error" in result
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(result, default=str))],
        isError=is_error,
    )


def build_graph_mcp_server() -> Server:
    """Construct the low-level MCP Server with the graph tool handlers registered.

    Argument validation stays with the shared ToolRegistry, which checks against
    each tool's Pydantic model and returns LLM-friendly structured errors — a
    more useful gate than a raw JSON Schema rejection.
    """
    server: Server = Server(
        SERVER_NAME,
        version=_server_version(),
        title="Microsoft Graph",
        website_url="https://github.com/nitin27may/ms-graph-mcp",
        on_list_tools=list_graph_tools,
        on_call_tool=dispatch_graph_tool,
    )
    read_count = len(READ_TOOL_NAME_SET)
    write_count = len(WRITE_TOOL_NAME_SET)
    logger.info(
        "graph-mcp server built with %d read + %d write tools (%d total)",
        read_count,
        write_count,
        read_count + write_count,
    )
    return server
