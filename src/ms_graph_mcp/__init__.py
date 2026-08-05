"""ms-graph-mcp — Microsoft Graph tool surface + MCP server (Streamable HTTP / stdio).

``client.py`` holds the raw httpx helpers (graph_get / graph_post / graph_patch /
graph_delete); one module per Graph domain (calendar, email, meetings, teams,
files, people, directory, tasks, onenote) hosts the ``@tool`` adapters. Importing
this package runs each domain module's ``@tool`` decorators so the adapters
register in the active ``ms_graph_mcp.tooling`` registry.

Run it as a server via the console scripts — ``ms-graph-mcp`` (stdio) or
``ms-graph-mcp-http`` (Streamable HTTP) — or embed the Starlette app directly:

    from ms_graph_mcp.app import build_app
    app = build_app()

The domain modules are also importable on their own, for callers that want the
Graph tools as plain async functions rather than over MCP:

    from ms_graph_mcp import calendar, email
    await calendar.get_upcoming_meetings(params, context)
"""

# Import each domain module for its @tool registration side effects + re-export.
from ms_graph_mcp import (  # noqa: E402  (after config so submodules see get_config)
    calendar,
    calendar_write,
    chats,
    contacts,
    directory,
    email,
    files,
    internal,
    meetings,
    onenote,
    people,
    search,
    tasks,
    tasks_write,
    teams,
)
from ms_graph_mcp.config import GraphMcpConfig, get_config, reset_config, set_config

__all__ = [
    # config seam
    "GraphMcpConfig",
    "get_config",
    "set_config",
    "reset_config",
    # domain tool modules
    "calendar",
    "calendar_write",
    "chats",
    "contacts",
    "directory",
    "email",
    "files",
    "internal",
    "meetings",
    "onenote",
    "people",
    "search",
    "tasks",
    "tasks_write",
    "teams",
]
