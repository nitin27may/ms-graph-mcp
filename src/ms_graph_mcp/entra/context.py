"""Request-scoped ContextVars for the verified principal + credentials.

``ServiceAuthMiddleware`` sets these at the start of every authenticated
request so tool implementations can read the caller's token / identity without
threading it through every signature. The backend's ``shared.context`` and the
MCP packages re-export these so there is a single source of truth.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing claims at runtime (no cycle, just tidy)
    from ms_graph_mcp.entra.claims import Principal

# The verified caller identity for the current request.
current_principal: ContextVar[Principal | None] = ContextVar(
    "wg_principal", default=None
)

# The principal's access token (user JWT at the edge, OBO token downstream).
current_access_token: ContextVar[str] = ContextVar("access_token", default="")

# The authenticated user's email (derived from the verified token).
current_user_email: ContextVar[str] = ContextVar("user_email", default="")

# The A2A contextId — shared across all tasks in a single workflow.
current_context_id: ContextVar[str] = ContextVar("context_id", default="")
