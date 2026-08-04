"""Microsoft Entra ID tools — user search, group membership, org structure."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get
from ms_graph_mcp.tooling import tool

logger = logging.getLogger(__name__)

_USER_SELECT = "id,displayName,mail,userPrincipalName,jobTitle,department,officeLocation,mobilePhone,businessPhones,companyName"


def _resolve_group_token(context: dict, tool_name: str) -> str:
    """Return the token to use for a group-scoped Graph call.

    Prefer the Entra **app-only** token (acquired via client credentials
    in ``executor_base.py`` from the ``ENTRA_CLIENT_*`` env vars).
    With a delegated/OBO token Graph silently returns ``displayName: null``
    on group typed-fields — the symptom that keeps surfacing as "AD group
    names are blank in the Entra cards".

    If the app-only token isn't in the context, fall back to the delegated
    token (so the call doesn't error) but log a loud warning so the
    misconfiguration is visible. The fallback path can still return ids +
    types; the caller's UI will just show empty names.
    """
    app_token = context.get("entra_app_token")
    if app_token:
        return app_token
    logger.warning(
        "[entra] %s: entra_app_token missing — falling back to delegated token. "
        "Group displayName will likely come back null. "
        "Verify the ENTRA_CLIENT_* environment variables are set on the "
        "entra agent so client-credentials acquisition succeeds.",
        tool_name,
    )
    return context["access_token"]


# ── Input models ─────────────────────────────────────────────────────────────


class SearchUsersInput(BaseModel):
    query: str = Field(description="User name, email, or UPN to search for", max_length=255)
    max_results: int = Field(20, description="Maximum results to return (1-50)", le=50)


class UserIdentifierInput(BaseModel):
    user: str = Field(description="User email address or User Principal Name (UPN)", max_length=320)


class SearchGroupsInput(BaseModel):
    query: str = Field(description="Group name or keyword to search for", max_length=255)
    max_results: int = Field(20, description="Maximum results to return (1-50)", le=50)


class GroupIdInput(BaseModel):
    group_id: str = Field(description="Entra group ID (GUID)", max_length=36)
    max_results: int = Field(50, description="Maximum members to return (1-200)", le=200)


# ── User tools (OBO — delegated token) ───────────────────────────────────────


@tool(
    description="Search Entra users by display name, email, or UPN. Returns profiles with job title, department, and office."
)
async def search_users(params: SearchUsersInput, context: dict) -> list[dict]:
    token = context["access_token"]
    q = params.query.strip()
    max_r = min(max(params.max_results, 1), 50)

    # Try exact email/UPN match first, fall back to search
    if "@" in q:
        try:
            user = await graph_get(token, f"/users/{q}", **{"$select": _USER_SELECT})
            if user and user.get("id"):
                return [_format_user(user)]
        except Exception:
            pass  # Not found by exact match, fall through to search

    # Search by displayName (ConsistencyLevel: eventual required for $search)
    headers = {"ConsistencyLevel": "eventual"}
    data = await graph_get(
        token,
        "/users",
        headers=headers,
        **{
            "$search": f'"displayName:{q}" OR "mail:{q}" OR "userPrincipalName:{q}"',
            "$select": _USER_SELECT,
            "$top": str(max_r),
            "$orderby": "displayName",
        },
    )

    users = data.get("value", [])
    return [_format_user(u) for u in users]


@tool(
    description="Get full user profile including manager information. Specify user by email or UPN."
)
async def get_user_details(params: UserIdentifierInput, context: dict) -> dict:
    token = context["access_token"]
    user_id = params.user.strip()

    # Fetch user with manager expanded
    data = await graph_get(
        token,
        f"/users/{user_id}",
        **{
            "$select": _USER_SELECT,
            "$expand": "manager($select=displayName,mail,jobTitle)",
        },
    )

    result = _format_user(data)

    # Add manager info if available
    manager = data.get("manager")
    if manager:
        result["manager"] = {
            "displayName": manager.get("displayName", ""),
            "mail": manager.get("mail", ""),
            "jobTitle": manager.get("jobTitle", ""),
        }

    return result


@tool(description="Get a user's manager. Returns the manager's profile.")
async def get_user_manager(params: UserIdentifierInput, context: dict) -> dict:
    token = context["access_token"]
    user_id = params.user.strip()

    try:
        data = await graph_get(
            token,
            f"/users/{user_id}/manager",
            **{"$select": _USER_SELECT},
        )
        return _format_user(data)
    except Exception as exc:
        if "404" in str(exc) or "Request_ResourceNotFound" in str(exc):
            return {
                "error": f"No manager found for '{user_id}'. They may be at the top of the org chart."
            }
        raise


@tool(
    description="List all Entra groups a user belongs to. Returns group names, types, and descriptions."
)
async def get_user_groups(params: UserIdentifierInput, context: dict) -> list[dict]:
    """List the groups a user is a direct member of.

    Two Graph gotchas this code intentionally works around — the same ones
    documented in ``orchestrator/rest/graph_routes.py:get_user_member_of``
    (the dedicated /entra page calls that route, which is why it works while
    a naive call here returned blank group names):

    1. ``/users/{upn}/memberOf`` returns a mixed-type ``directoryObject``
       collection (groups + directory roles + administrative units). When
       you apply ``$select=displayName,…`` to the uncast collection Graph
       returns ``null`` for every typed-only field on every row — symptom:
       group names blank in the UI. Fix: cast the path to
       ``/microsoft.graph.group`` so ``$select`` resolves against the
       group schema for every row. The cast also makes the Python-side
       ``@odata.type`` filter unnecessary.

    2. With a delegated (OBO) token the "limited information" rule applies
       and Graph returns 200 with every typed field still ``null`` even on
       the cast endpoint. Fix: prefer the entra_app app-only token (same
       pattern as ``search_groups`` / ``get_group_members`` below). OBO
       stays as a fallback for tenants that haven't configured the Entra
       app — they'll still get group ids + types, just not displayName.
    """
    token = _resolve_group_token(context, "get_user_groups")
    user_id = params.user.strip()

    data = await graph_get(
        token,
        f"/users/{user_id}/memberOf/microsoft.graph.group",
        headers={"ConsistencyLevel": "eventual"},
        **{
            "$select": "id,displayName,description,groupTypes,securityEnabled,mailEnabled,mail",
            "$top": "100",
            "$count": "true",
        },
    )

    return [_format_group(g) for g in data.get("value", [])]


# ── Group tools (client credentials — app-only token) ────────────────────────


@tool(
    description="Search Entra groups by display name. Returns group name, type, description, and member count."
)
async def search_groups(params: SearchGroupsInput, context: dict) -> list[dict]:
    # Use app-only token for tenant-wide group search; OBO falls through
    # to null displayName.
    token = _resolve_group_token(context, "search_groups")
    q = params.query.strip()
    max_r = min(max(params.max_results, 1), 50)

    headers = {"ConsistencyLevel": "eventual"}
    data = await graph_get(
        token,
        "/groups",
        headers=headers,
        **{
            "$search": f'"displayName:{q}"',
            "$select": "id,displayName,description,groupTypes,securityEnabled,mailEnabled,createdDateTime",
            "$top": str(max_r),
            "$orderby": "displayName",
            "$count": "true",
        },
    )

    return [_format_group(g) for g in data.get("value", [])]


@tool(
    description="List all members of an Entra group. Returns user profiles. Use group_id from search_groups."
)
async def get_group_members(params: GroupIdInput, context: dict) -> dict:
    token = _resolve_group_token(context, "get_group_members")
    max_r = min(max(params.max_results, 1), 200)

    data = await graph_get(
        token,
        f"/groups/{params.group_id}/members",
        **{
            "$select": _USER_SELECT,
            "$top": str(max_r),
        },
    )

    members = []
    for item in data.get("value", []):
        odata_type = item.get("@odata.type", "")
        if "#microsoft.graph.user" in odata_type:
            members.append(_format_user(item))
        else:
            # Service principals, nested groups, etc.
            members.append(
                {
                    "displayName": item.get("displayName", "Unknown"),
                    "type": odata_type.split(".")[-1] if odata_type else "Unknown",
                }
            )

    # Get total count (may differ from page size)
    total_count = data.get("@odata.count", len(members))
    next_link = data.get("@odata.nextLink")

    return {
        "members": members,
        "total_count": total_count,
        "returned_count": len(members),
        "has_more": next_link is not None,
    }


@tool(description="Get details of an Entra group including type, description, and creation date.")
async def get_group_details(params: GroupIdInput, context: dict) -> dict:
    token = _resolve_group_token(context, "get_group_details")

    data = await graph_get(
        token,
        f"/groups/{params.group_id}",
        **{
            "$select": "id,displayName,description,groupTypes,securityEnabled,mailEnabled,createdDateTime,mail",
        },
    )

    result = _format_group(data)

    # Get member count
    try:
        count_data = await graph_get(
            token,
            f"/groups/{params.group_id}/members/$count",
            headers={"ConsistencyLevel": "eventual"},
        )
        result["member_count"] = count_data if isinstance(count_data, int) else 0
    except Exception:
        result["member_count"] = None

    return result


# ── Helpers ──────────────────────────────────────────────────────────────────


def _format_user(data: dict) -> dict:
    """Normalize a Graph user object to a clean dict."""
    phones = data.get("businessPhones", [])
    return {
        "displayName": data.get("displayName", ""),
        "mail": data.get("mail") or data.get("userPrincipalName", ""),
        "userPrincipalName": data.get("userPrincipalName", ""),
        "jobTitle": data.get("jobTitle", ""),
        "department": data.get("department", ""),
        "officeLocation": data.get("officeLocation", ""),
        "mobilePhone": data.get("mobilePhone", ""),
        "businessPhone": phones[0] if phones else "",
        "companyName": data.get("companyName", ""),
    }


def _format_group(data: dict) -> dict:
    """Normalize a Graph group object to a clean dict."""
    group_types = data.get("groupTypes", [])
    security = data.get("securityEnabled", False)
    mail_enabled = data.get("mailEnabled", False)

    if "Unified" in group_types:
        gtype = "Microsoft 365"
    elif security and not mail_enabled:
        gtype = "Security"
    elif security and mail_enabled:
        gtype = "Mail-enabled Security"
    elif mail_enabled:
        gtype = "Distribution"
    else:
        gtype = "Other"

    return {
        "id": data.get("id", ""),
        "displayName": data.get("displayName", ""),
        "description": data.get("description", ""),
        "type": gtype,
        "mail": data.get("mail", ""),
        "createdDateTime": data.get("createdDateTime", ""),
    }
