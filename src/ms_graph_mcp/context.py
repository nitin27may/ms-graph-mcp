"""Request-scoped tool context for the graph-mcp server.

``GraphMcpAuthMiddleware`` populates this ContextVar with the per-request
credential dict; the MCP call-tool handler reads it to build the ``context``
argument passed to each ``@tool`` function. The default empty dict is only
ever read, never mutated.
"""

from __future__ import annotations

from contextvars import ContextVar

current_request_context: ContextVar[dict] = ContextVar("graph_mcp_request_context", default={})
