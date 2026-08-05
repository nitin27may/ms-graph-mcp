"""Unified search over Microsoft 365 via ``POST /search/query``.

One endpoint searches mail, calendar, files, SharePoint sites and lists, and
people. It is the highest-coverage single call in Graph, and the right first
move for a vague ask — "find the claims deck Priya sent me" touches chat, files
and mail, and none of the per-workload tools spans that.

``teams.py:chat_search_messages`` already uses this endpoint for ``chatMessage``
specifically; this generalises it.
"""

from __future__ import annotations

from enum import StrEnum

import httpx
from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_post
from ms_graph_mcp.errors import graph_error_response, invalid_arguments
from ms_graph_mcp.tooling import READ_ONLY, tool


class SearchEntity(StrEnum):
    """What to search. Graph will not mix arbitrary combinations."""

    message = "message"
    event = "event"
    drive_item = "driveItem"
    list_item = "listItem"
    site = "site"
    person = "person"


# Graph refuses a request that mixes these groups, with an error that does not
# explain why. Checking locally turns a confusing 400 into a clear instruction.
_MAIL_CALENDAR = {SearchEntity.message, SearchEntity.event}
_SHAREPOINT = {SearchEntity.drive_item, SearchEntity.list_item, SearchEntity.site}

_SCOPE_FOR = {
    SearchEntity.message: "Mail.Read",
    SearchEntity.event: "Calendars.Read",
    SearchEntity.drive_item: "Files.Read.All",
    SearchEntity.list_item: "Sites.Read.All",
    SearchEntity.site: "Sites.Read.All",
    SearchEntity.person: "People.Read",
}


class SearchQueryInput(BaseModel):
    query: str = Field(description="What to search for. Supports KQL, e.g. 'budget filetype:xlsx'.")
    entity_types: list[SearchEntity] = Field(
        default_factory=lambda: [SearchEntity.drive_item],
        description="What to search: message, event, driveItem, listItem, site, person",
    )
    max_results: int = Field(default=15, description="Maximum hits to return (1-25)")


def _summarise_hit(hit: dict) -> dict:
    """Flatten one search hit into something a model can read.

    Graph returns a full resource per hit, and the shape differs per entity
    type. Passing those through raw would cost enormous tokens for fields
    nothing needs.
    """
    resource = hit.get("resource") or {}
    kind = (resource.get("@odata.type") or "").rsplit(".", 1)[-1]
    out = {
        "type": kind,
        "summary": (hit.get("summary") or "").strip()[:400],
        "rank": hit.get("rank"),
    }
    # Per-type identifying fields, chosen so the model can act on the result.
    if title := (resource.get("subject") or resource.get("name") or resource.get("displayName")):
        out["title"] = title
    if url := (resource.get("webUrl") or resource.get("webLink")):
        out["url"] = url
    if rid := resource.get("id"):
        out["id"] = rid
    if sender := ((resource.get("from") or {}).get("emailAddress") or {}).get("address"):
        out["from"] = sender
    if received := resource.get("receivedDateTime") or resource.get("lastModifiedDateTime"):
        out["date"] = received
    if parent := (resource.get("parentReference") or {}).get("driveId"):
        out["drive_id"] = parent
    return out


@tool(
    description=(
        "Search across Microsoft 365 in one call — email, calendar events, OneDrive and SharePoint "
        "files, sites, lists and people. Best first move for a vague request, because it spans "
        "workloads no single other tool covers. Choose entity types to narrow it. Supports KQL "
        "such as 'budget filetype:xlsx'. Permissions vary by entity type searched."
    ),
    annotations=READ_ONLY,
)
async def search_query(params: SearchQueryInput, context: dict) -> dict:
    token = context["access_token"]
    if not params.entity_types:
        return invalid_arguments("Specify at least one entity type to search.")
    requested = set(params.entity_types)
    if requested & _MAIL_CALENDAR and requested & _SHAREPOINT:
        return invalid_arguments(
            "Microsoft Graph cannot search mail/calendar and SharePoint/OneDrive in the same "
            "request. Split this into two calls: one for message and event, another for "
            "driveItem, listItem and site."
        )
    body = {
        "requests": [
            {
                "entityTypes": [e.value for e in params.entity_types],
                "query": {"queryString": params.query},
                "from": 0,
                "size": min(max(params.max_results, 1), 25),
            }
        ]
    }
    try:
        data = await graph_post(token, "/search/query", body)
    except httpx.HTTPStatusError as exc:
        # Name the scope for whichever type was asked for first — better than a
        # bare "the required permission" when several could apply.
        scope = _SCOPE_FOR.get(params.entity_types[0], "")
        return graph_error_response(exc, scope=scope, tool="search_query")

    hits: list[dict] = []
    total = 0
    for response in data.get("value") or []:
        for container in response.get("hitsContainers") or []:
            total += container.get("total", 0) or 0
            hits.extend(_summarise_hit(h) for h in (container.get("hits") or []))
    return {"hits": hits, "total_matches": total, "returned": len(hits)}


__all__ = ["search_query"]
