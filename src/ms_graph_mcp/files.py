"""Graph Files tools — OneDrive/SharePoint file search and content retrieval."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get, graph_get_url, graph_try_get
from ms_graph_mcp.config import get_config
from ms_graph_mcp.tooling import READ_ONLY, tool

logger = logging.getLogger(__name__)

_SELECT_FILE = "id,name,webUrl,lastModifiedDateTime,lastModifiedBy,size,file,parentReference"
# Folder browser navigation needs a folder marker (`folder` non-null) to
# branch list rows into navigable vs leaf, plus eTag/cTag for dedup
# downstream and createdDateTime for "modified" display.
_SELECT_FOLDER_LIST = (
    "id,name,webUrl,size,file,folder,parentReference,lastModifiedDateTime,createdDateTime,eTag,cTag"
)


class SearchFilesInput(BaseModel):
    query: str = Field(description="Search keywords to find files and documents")
    max_results: int = Field(10, description="Maximum files to return")


class GetTrendingFilesInput(BaseModel):
    max_results: int = Field(10, description="Maximum trending files to return")


class GetRecentFilesInput(BaseModel):
    max_results: int = Field(10, description="Maximum recently accessed files to return")


class GetFileContentInput(BaseModel):
    drive_id: str = Field(description="The OneDrive/SharePoint drive ID")
    item_id: str = Field(description="The file item ID")
    max_chars: int = Field(4000, description="Maximum characters of content to extract")


class GetSharedFilesInput(BaseModel):
    max_results: int = Field(10, description="Maximum shared-with-me files to return")


@tool(
    description=(
        "Search the signed-in user's OneDrive and the SharePoint sites they can reach, by "
        "filename or content keyword. Returns id, name, drive id, size, last-modified date and "
        "web URL. OneDrive and SharePoint document libraries are the same underlying resource, "
        "so one search covers both. Requires Files.Read.All."
    ),
    annotations=READ_ONLY,
    aliases=("search_files",),
)
async def files_search(params: SearchFilesInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me/drive/root/search(q='{q}')".replace("{q}", params.query),
        **{"$select": _SELECT_FILE, "$top": min(params.max_results, 25)},
    )
    return [_slim_file(f) for f in (data.get("value") or []) if f.get("file")]


@tool(
    description=(
        "List documents currently trending around the signed-in user — files their colleagues "
        "are actively working on, ranked by Microsoft 365 activity signals rather than by the "
        "user's own history. Use for 'what is the team working on'. files_list_recent is the "
        "tool for what this user personally touched. Requires Files.Read.All."
    ),
    annotations=READ_ONLY,
    aliases=("get_trending_files",),
)
async def files_list_trending(params: GetTrendingFilesInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me/insights/trending",
        **{
            "$select": "id,resourceVisualization,resourceReference",
            "$top": min(params.max_results, 20),
        },
    )
    return [
        {
            "id": i.get("id", ""),
            "name": (i.get("resourceVisualization") or {}).get("title", ""),
            "type": (i.get("resourceVisualization") or {}).get("type", ""),
            "url": (i.get("resourceReference") or {}).get("webUrl", ""),
            "container": (i.get("resourceVisualization") or {}).get("containerDisplayName", ""),
        }
        for i in (data.get("value") or [])
    ]


@tool(
    description=(
        "List files the signed-in user has recently opened or edited, most recent first. "
        "Returns id, name, drive id and web URL. Use for 'the document I was working on "
        "yesterday' — files_search is better when the name or a keyword is known, and "
        "files_list_trending covers what colleagues are working on. Requires Files.Read."
    ),
    annotations=READ_ONLY,
    aliases=("get_recent_files",),
)
async def files_list_recent(params: GetRecentFilesInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me/drive/recent",
        **{"$select": _SELECT_FILE, "$top": min(params.max_results, 20)},
    )
    return [_slim_file(f) for f in (data.get("value") or []) if f.get("file")]


@tool(
    description=(
        "Read the text content of a file in OneDrive or SharePoint, given a drive id and item "
        "id from files_search or files_list_recent. Works for text, Office documents and PDFs; "
        "binary files return a placeholder instead of bytes. Output is truncated to a caller-"
        "specified character limit. Requires Files.Read.All."
    ),
    annotations=READ_ONLY,
    aliases=("get_file_content",),
)
async def files_get_content(params: GetFileContentInput, context: dict) -> dict:
    token = context["access_token"]

    # /content answers 302 with a short-lived download URL on another host, so
    # the redirect has to be followed. A non-200 is reported to the model rather
    # than raised — a file it cannot read is an answer, not a crash.
    resp = await graph_try_get(
        token,
        f"/drives/{params.drive_id}/items/{params.item_id}/content",
        accept="*/*",
        timeout_seconds=60,
        follow_redirects=True,
    )
    if resp.ok:
        if "text" in resp.content_type or "json" in resp.content_type:
            text = resp.text[: params.max_chars]
        else:
            text = "[Binary file — text extraction not available]"
    else:
        text = f"[Could not retrieve content: HTTP {resp.status_code}]"

    # Get metadata
    meta = await graph_get(
        token,
        f"/drives/{params.drive_id}/items/{params.item_id}",
        **{"$select": _SELECT_FILE},
    )
    return {
        "name": meta.get("name", ""),
        "url": meta.get("webUrl", ""),
        "size": meta.get("size", 0),
        "last_modified": meta.get("lastModifiedDateTime", ""),
        "content": text,
    }


@tool(
    description=(
        "List files other people have shared directly with the signed-in user, with the owner "
        "and web URL for each. Covers items shared via OneDrive or SharePoint that do not live "
        "in the user's own drive, which is why files_list_recent and files_search may not "
        "surface them. Requires Files.Read.All."
    ),
    annotations=READ_ONLY,
    aliases=("get_shared_files",),
)
async def files_list_shared_with_me(params: GetSharedFilesInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me/drive/sharedWithMe",
        **{"$select": _SELECT_FILE, "$top": min(params.max_results, 20)},
    )
    return [_slim_file(f) for f in (data.get("value") or []) if f.get("file")]


# ── Group / team drive resolution ────────────────────────────────────────────


class GetGroupDriveInput(BaseModel):
    group_id: str = Field(description="Microsoft 365 group (Team) id — same as team.id")


@tool(
    description=(
        "Get the document library backing a Microsoft 365 group or Teams team, given a group id "
        "from directory_search_groups. Returns the drive id and web URL, which is what the "
        "other file tools need to read or write inside a team's shared files. Requires "
        "Files.Read.All and Group.Read.All."
    ),
    annotations=READ_ONLY,
    aliases=("get_group_drive",),
)
async def files_get_group_drive(params: GetGroupDriveInput, context: dict) -> dict:
    token = context["access_token"]
    data = await graph_get(
        token,
        f"/groups/{params.group_id}/drive",
        **{"$select": "id,name,webUrl,driveType"},
    )
    return {
        "drive_id": data.get("id", ""),
        "name": data.get("name", ""),
        "web_url": data.get("webUrl", ""),
        "drive_type": data.get("driveType", ""),
    }


# ── Folder browser (KB seed wizard "Browse folders" mode) ────────────────────
#
# The seed wizard's folder-picker UI calls these via the Next.js proxy at
# /api/graph/onedrive/children (server-side, OBO token never reaches the
# browser). The walker is also called by gather_browse_selection at scan
# time to expand picked folders into leaf files.


async def list_drive_item_children(
    access_token: str,
    drive_id: str,
    item_id: str,
    *,
    top: int = 200,
) -> list[dict]:
    """List immediate children of a OneDrive/SharePoint folder.

    Pages through Graph's @odata.nextLink internally. Returns raw Graph
    driveItem objects (caller normalises). When ``item_id == "root"``
    targets ``/me/drive/root/children``; otherwise
    ``/drives/{drive_id}/items/{item_id}/children``.
    """
    if item_id == "root":
        path = "/me/drive/root/children"
    else:
        path = f"/drives/{drive_id}/items/{item_id}/children"
    items: list[dict] = []
    page = await graph_get(access_token, path, **{"$select": _SELECT_FOLDER_LIST, "$top": top})
    items.extend(page.get("value") or [])
    next_link = page.get("@odata.nextLink")
    # Graph returns absolute URLs in nextLink — graph_get only takes a
    # relative path + params, so for paging we drop back to httpx
    # directly. Most folders fit in one page; this branch is for the
    # rare oversize case.
    while next_link:
        payload = await graph_get_url(access_token, next_link)
        items.extend(payload.get("value") or [])
        next_link = payload.get("@odata.nextLink")
    return items


async def walk_drive_descendants(
    access_token: str,
    drive_id: str,
    item_id: str,
    *,
    max_files: int | None = None,
) -> AsyncIterator[dict]:
    """BFS walk of a folder, yielding **file** driveItems only.

    Folders are traversed but not yielded. Stops emitting once
    ``max_files`` leaf files have been produced (folders queued past
    the cap are abandoned). Yields raw Graph driveItem dicts so the
    caller can extract whatever metadata it needs.
    """
    if max_files is None:
        max_files = get_config().browse_max_files
    yielded = 0
    queue: list[tuple[str, str]] = [(drive_id, item_id)]
    while queue and yielded < max_files:
        cur_drive, cur_id = queue.pop(0)
        try:
            children = await list_drive_item_children(access_token, cur_drive, cur_id)
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the scan
            logger.warning(
                "[walk_drive_descendants] children fetch failed drive=%s item=%s: %s",
                cur_drive,
                cur_id,
                exc,
            )
            continue
        for child in children:
            if child.get("folder"):
                child_drive = (child.get("parentReference") or {}).get("driveId") or cur_drive
                queue.append((child_drive, child.get("id") or ""))
            elif child.get("file"):
                if yielded >= max_files:
                    return
                yield child
                yielded += 1


# ── Helpers ───────────────────────────────────────────────────────────────────


def _slim_file(f: dict) -> dict:
    modifier = (f.get("lastModifiedBy") or {}).get("user", {})
    parent = f.get("parentReference") or {}
    return {
        "id": f.get("id", ""),
        "drive_id": parent.get("driveId", ""),
        "name": f.get("name", ""),
        "url": f.get("webUrl", ""),
        "size_kb": round((f.get("size") or 0) / 1024, 1),
        "last_modified": f.get("lastModifiedDateTime", ""),
        "modified_by": modifier.get("displayName", ""),
        "site": parent.get("siteId", ""),
    }
