"""graph-mcp stdio transport — for local MCP clients (e.g. Claude Desktop).

stdio has no per-request HTTP headers, so the single session's Graph credentials
come from the environment and are stashed into the request context once at
startup:

  - ``GRAPH_MCP_ACCESS_TOKEN``  the delegated Graph token tools run on
  - ``GRAPH_MCP_USER_EMAIL``    the caller's email (tenant-isolation key)
  - ``GRAPH_MCP_WRITE_SCOPE``   "true" to expose + allow the write tools

Run via the ``ms-graph-mcp`` console script.
"""

from __future__ import annotations

import os

import anyio
from mcp.server.stdio import stdio_server

from ms_graph_mcp.context import current_request_context
from ms_graph_mcp.server import build_graph_mcp_server


async def _amain() -> None:
    current_request_context.set(
        {
            "access_token": os.getenv("GRAPH_MCP_ACCESS_TOKEN", ""),
            "user_email": os.getenv("GRAPH_MCP_USER_EMAIL", ""),
            "write_scope": os.getenv("GRAPH_MCP_WRITE_SCOPE", "").lower() == "true",
        }
    )
    server = build_graph_mcp_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """Console entry point (``ms-graph-mcp``) — run the stdio server."""
    anyio.run(_amain)
