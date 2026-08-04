"""Graph Files tools — OneDrive/SharePoint file search and content retrieval."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get
from ms_graph_mcp.config import get_config
from ms_graph_mcp.tooling import tool

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
    description="Search OneDrive and SharePoint for files matching keywords. Returns file name, URL, and last modified date."
)
async def search_files(params: SearchFilesInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me/drive/root/search(q='{q}')".replace("{q}", params.query),
        **{"$select": _SELECT_FILE, "$top": min(params.max_results, 25)},
    )
    return [_slim_file(f) for f in (data.get("value") or []) if f.get("file")]


@tool(description="Get files trending around the user based on their recent activity and network.")
async def get_trending_files(params: GetTrendingFilesInput, context: dict) -> list[dict]:
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


@tool(description="Get files the user has recently viewed or edited.")
async def get_recent_files(params: GetRecentFilesInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me/drive/recent",
        **{"$select": _SELECT_FILE, "$top": min(params.max_results, 20)},
    )
    return [_slim_file(f) for f in (data.get("value") or []) if f.get("file")]


@tool(
    description="Get the text content of a file from OneDrive or SharePoint. Supports Word, PDF, and plain text files."
)
async def get_file_content(params: GetFileContentInput, context: dict) -> dict:
    token = context["access_token"]
    import httpx

    # Try to get text rendition (works for Office files and PDFs)
    content_url = (
        f"https://graph.microsoft.com/v1.0/drives/{params.drive_id}/items/{params.item_id}/content"
    )
    async with httpx.AsyncClient(
        verify=not get_config().disable_ssl_verify, timeout=60, follow_redirects=True
    ) as client:
        resp = await client.get(
            content_url,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 200:
            content_type = resp.headers.get("content-type", "")
            if "text" in content_type or "json" in content_type:
                text = resp.text[: params.max_chars]
            else:
                # Binary file — return metadata only
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


@tool(description="Get files that other people have shared with the user.")
async def get_shared_files(params: GetSharedFilesInput, context: dict) -> list[dict]:
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
        "Resolve a Microsoft 365 group (Team) to its default document-library "
        "SharePoint drive. Returns {drive_id, name, web_url, drive_type}."
    )
)
async def get_group_drive(params: GetGroupDriveInput, context: dict) -> dict:
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
    if next_link:
        import httpx

        from ms_graph_mcp.client import _headers  # type: ignore[attr-defined]

        async with httpx.AsyncClient(verify=not get_config().disable_ssl_verify, timeout=30) as client:
            while next_link:
                resp = await client.get(next_link, headers=_headers(access_token))
                resp.raise_for_status()
                payload = resp.json()
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
