"""App-Role authorization over a verified :class:`Principal`.

Authorization is "has at least one of the required App Roles" (ANY semantics),
so an agent can accept e.g. both ``meeting-prep.user`` and ``meeting-prep.admin``.
An empty required set means authenticate-only. App-only (client-credentials)
tokens are rejected unless ``allow_app_only`` is set (future automation agents).
"""

from __future__ import annotations

from ms_graph_mcp.entra.claims import Principal


def check_roles(
    principal: Principal,
    required: set[str],
    allow_app_only: bool = False,
) -> bool:
    """Return True if ``principal`` is authorized for the given role policy."""
    if principal.is_app_only and not allow_app_only:
        return False
    if not required:
        return True  # authenticate-only
    return bool(required & principal.roles)
