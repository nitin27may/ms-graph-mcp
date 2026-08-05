"""Microsoft Graph tool registry for the graph-mcp server.

Tools are split into two tiers:

  READ  — always advertised and callable (safe, no side effects).
  WRITE — advertised and callable only when the caller explicitly opts in
          via the ``X-Write-Scope: true`` request header. This prevents
          accidental mutation from LLM loops that haven't been granted
          write intent.

``resolve_read_tools()`` and ``resolve_write_tools()`` validate against
the shared ToolRegistry at startup — a drift between this file and
``integrations/graph`` fails loudly.
"""

from __future__ import annotations

# Importing the package runs every graph module's @tool decorators, which
# register the adapters in the global ToolRegistry.
import ms_graph_mcp  # noqa: F401  (imported for @tool registration side effects)
from ms_graph_mcp.tooling import ToolSpec, get_registry

# ── Write tool allowlist ──────────────────────────────────────────────────────
# Gated behind X-Write-Scope header. Callers must explicitly request write
# access; the default surface is read-only.
WRITE_TOOL_NAMES: tuple[str, ...] = (
    "send_email",
    "propose_email",
    "save_to_onenote",
    "create_todo_task",
    # files
    "upload_file",
    "update_file_content",
    "create_folder",
    "create_sharing_link",
)

WRITE_TOOL_NAME_SET: frozenset[str] = frozenset(WRITE_TOOL_NAMES)

# Explicit read-only allowlist, grouped by Graph domain.
READ_TOOL_NAMES: tuple[str, ...] = (
    # calendar
    "get_upcoming_meetings",
    "get_meeting_details",
    "get_meeting_attendees",
    "get_calendar_events_range",
    # email
    "list_email_attachments",
    "search_emails",
    "get_recent_emails",
    "get_flagged_emails",
    "get_email_thread",
    # meetings
    "get_meeting_transcript",
    "get_past_meetings",
    "get_online_meeting_from_event",
    "list_meeting_transcripts",
    "get_meetings_with_transcripts",
    "get_transcript_by_event_id",
    "get_attendance_report",
    # files
    "search_files",
    "get_trending_files",
    "get_recent_files",
    "get_file_content",
    "get_shared_files",
    # people
    "search_people",
    "get_person_details",
    "get_my_profile",
    # directory
    "search_users",
    "get_user_details",
    "get_user_manager",
    "get_user_groups",
    "search_groups",
    "get_group_members",
    "get_group_details",
    # teams
    "search_teams_messages",
    "get_channel_messages",
    "get_joined_teams",
    "get_team_channels",
    "get_group_drive",
    # onenote
    "get_notebooks",
    "get_sections",
    # tasks
    "get_planner_plans",
    "get_planner_buckets",
    "get_todo_lists",
    "get_todo_tasks",
    "get_planner_tasks",
)

READ_TOOL_NAME_SET: frozenset[str] = frozenset(READ_TOOL_NAMES)

# ── Internal (deterministic) tool tier ────────────────────────────────────────
# For the host application's OWN ETL / workers / REST routes / write-sinks calling
# the MCP as plain functions over HTTP. Advertised + callable ONLY when the request
# carries the internal scope (``X-Internal-Scope: true``), which auth.py honours
# only for the machine-secret principal. NEVER exposed to LLM agents or external
# MCP clients — kept strictly out of READ/WRITE.
INTERNAL_TOOL_NAMES: tuple[str, ...] = (
    "graph_request",  # narrow gated passthrough for arbitrary-path callers
    # Named internal tools — reusable Graph ops the passthrough can't express.
    "graph_walk_drive_descendants",
    "graph_ensure_drive_folder",
    "graph_upload_file_to_drive",
    "graph_update_drive_item_content",
    "fetch_message_attachments",
    "graph_get_group_drive",
    "graph_download_drive_item",
    # App-only (no user token) — RBAC revalidation access probe.
    "probe_graph_access",
)

INTERNAL_TOOL_NAME_SET: frozenset[str] = frozenset(INTERNAL_TOOL_NAMES)

# App-only internal tools — dispatch mints a client-credentials token for these
# and skips the user-token guard + OBO. The revalidation sweep has no user session.
APP_ONLY_INTERNAL_TOOL_NAMES: tuple[str, ...] = ("probe_graph_access",)
APP_ONLY_INTERNAL_TOOL_NAME_SET: frozenset[str] = frozenset(APP_ONLY_INTERNAL_TOOL_NAMES)

# ── Combined set (for dispatch validation) ────────────────────────────────────
ALL_TOOL_NAME_SET: frozenset[str] = (
    READ_TOOL_NAME_SET | WRITE_TOOL_NAME_SET | INTERNAL_TOOL_NAME_SET
)


def assert_no_write_in_reads() -> None:
    """Fail loudly if a write/internal tool ever leaks into the read allowlist."""
    leaked = WRITE_TOOL_NAME_SET & READ_TOOL_NAME_SET
    if leaked:
        raise RuntimeError(
            f"graph-mcp read allowlist contains write tools: {sorted(leaked)}. "
            "Write tools must never be exposed over the read-only MCP surface."
        )
    internal_leak = INTERNAL_TOOL_NAME_SET & (READ_TOOL_NAME_SET | WRITE_TOOL_NAME_SET)
    if internal_leak:
        raise RuntimeError(
            f"graph-mcp internal tools leaked into the agent surface: {sorted(internal_leak)}. "
            "Internal tools must never appear in READ/WRITE (agent/external) allowlists."
        )
    if len(READ_TOOL_NAMES) != len(READ_TOOL_NAME_SET):
        raise RuntimeError("graph-mcp read allowlist contains duplicate tool names.")
    if len(INTERNAL_TOOL_NAMES) != len(INTERNAL_TOOL_NAME_SET):
        raise RuntimeError("graph-mcp internal allowlist contains duplicate tool names.")


def resolve_read_tools() -> list[ToolSpec]:
    """Resolve the read allowlist to registered ToolSpecs.

    Raises if a name is missing from the registry — that means
    ``integrations/graph`` drifted from this allowlist, and the server must
    not start half-wired.
    """
    assert_no_write_in_reads()
    registry = get_registry()
    specs: list[ToolSpec] = []
    missing: list[str] = []
    for name in READ_TOOL_NAMES:
        spec = registry.get(name)
        if spec is None:
            missing.append(name)
        else:
            specs.append(spec)
    if missing:
        raise RuntimeError(
            f"graph-mcp allowlist references tools absent from the registry: {missing}. "
            "integrations/graph drifted from agents/graph_mcp/tools.py."
        )
    return specs


def resolve_write_tools() -> list[ToolSpec]:
    """Resolve the write allowlist to registered ToolSpecs.

    Same safety guarantees as ``resolve_read_tools``.
    """
    registry = get_registry()
    specs: list[ToolSpec] = []
    missing: list[str] = []
    for name in WRITE_TOOL_NAMES:
        spec = registry.get(name)
        if spec is None:
            missing.append(name)
        else:
            specs.append(spec)
    if missing:
        raise RuntimeError(
            f"graph-mcp write allowlist references tools absent from the registry: {missing}. "
            "integrations/graph drifted from agents/graph_mcp/tools.py."
        )
    return specs


def resolve_internal_tools() -> list[ToolSpec]:
    """Resolve the internal (deterministic) allowlist to registered ToolSpecs.

    Same safety guarantees as ``resolve_read_tools``.
    """
    registry = get_registry()
    specs: list[ToolSpec] = []
    missing: list[str] = []
    for name in INTERNAL_TOOL_NAMES:
        spec = registry.get(name)
        if spec is None:
            missing.append(name)
        else:
            specs.append(spec)
    if missing:
        raise RuntimeError(
            f"graph-mcp internal allowlist references tools absent from the registry: {missing}. "
            "ms_graph_mcp.internal drifted from allowlists.py."
        )
    return specs
