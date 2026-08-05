"""
OneDrive write helpers — file uploads + folder creation.

All endpoints run on the user's delegated token (OBO). Used by:

  * ``integrations.action_sinks.onedrive`` — creates a new file
  * orchestrator REST ``/api/intel/document/sync-to-onedrive`` — updates
    an existing file in place so the same itemId stays referenced across
    in-session edits

Two kinds of conflicts:

  * 409 Conflict on create — folder already exists. Swallowed inside
    ``ensure_folder_exists``; surfaced from ``upload_file_to_drive`` as
    ``OneDriveError`` so the caller can decide.
  * 412 Precondition Failed on overwrite — ``If-Match`` eTag mismatch.
    The user edited the OneDrive copy externally; surfaced as a typed
    ``OneDriveConflictError`` so the UI can show a "force overwrite?"
    toast and *not* clobber the remote change blindly.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

import httpx
from opentelemetry import trace
from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get, graph_post, graph_put_raw
from ms_graph_mcp.config import get_config
from ms_graph_mcp.odata import validate_graph_id
from ms_graph_mcp.tooling import WRITE_CREATE, WRITE_UPDATE, tool

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("ms_graph_mcp")

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TIMEOUT = httpx.Timeout(60.0)
# Graph's documented threshold for the simple PUT path. Files above this
# need a createUploadSession + chunked upload.
_SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
_CHUNK_SIZE = 5 * 1024 * 1024


class OneDriveError(Exception):
    """Generic OneDrive write failure."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OneDriveConflictError(OneDriveError):
    """412 Precondition Failed — the existing item's eTag has changed."""

    def __init__(self, message: str = "OneDrive item changed since last sync") -> None:
        super().__init__(message, status_code=412)


_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def sanitize_filename(name: str, *, default: str = "document") -> str:
    """Strip path traversal + OneDrive-illegal chars; cap at 200 chars.

    OneDrive rejects ``< > : " / \\ | ? *`` plus leading/trailing dots
    and reserved Windows names. Illegal chars become ``-``, runs are
    collapsed, and the extension is preserved when truncating.
    """
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("-", name).strip(" .")
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if not cleaned:
        return default
    if len(cleaned) > 200:
        if "." in cleaned[-12:]:
            stem, _, ext = cleaned.rpartition(".")
            cleaned = stem[: 200 - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:200]
    return cleaned


def _normalise_folder_path(path: str) -> str:
    """Strip slashes and URL-encode each segment for the Graph path syntax."""
    parts = [p for p in path.split("/") if p]
    return "/".join(urllib.parse.quote(p, safe="") for p in parts)


def _drive_base(drive_id: str | None) -> str:
    """Return the Graph path prefix for ``/me/drive`` (personal) or ``/drives/{id}`` (any)."""
    return f"/drives/{drive_id}" if drive_id else "/me/drive"


def _build_simple_upload_path(
    folder_path: str,
    filename: str,
    *,
    conflict_behavior: str = "rename",
    drive_id: str | None = None,
) -> str:
    """Graph-relative path for a simple (<=4 MiB) content PUT."""
    folder_norm = _normalise_folder_path(folder_path)
    file_norm = urllib.parse.quote(filename, safe="")
    base = _drive_base(drive_id)
    if folder_norm:
        endpoint = f"{base}/root:/{folder_norm}/{file_norm}:/content"
    else:
        endpoint = f"{base}/root:/{file_norm}:/content"
    suffix = ""
    if conflict_behavior:
        suffix = (
            f"?@microsoft.graph.conflictBehavior={urllib.parse.quote(conflict_behavior, safe='')}"
        )
    return f"{endpoint}{suffix}"


def _slim_drive_item(item: dict[str, Any]) -> dict[str, Any]:
    parent = item.get("parentReference") or {}
    return {
        "id": item.get("id", ""),
        "drive_id": parent.get("driveId", ""),
        "name": item.get("name", ""),
        "web_url": item.get("webUrl", ""),
        "size": item.get("size", 0),
        "etag": item.get("eTag") or item.get("@odata.etag") or "",
        "last_modified": item.get("lastModifiedDateTime", ""),
        "mime_type": (item.get("file") or {}).get("mimeType", ""),
    }


async def ensure_folder_exists(
    access_token: str,
    folder_path: str,
    *,
    drive_id: str | None = None,
) -> dict[str, Any]:
    """Create ``folder_path`` under the drive root if missing.

    ``folder_path`` is forward-slash separated, root-relative
    (``"Reports/2026"``). Each missing intermediate is created.
    When ``drive_id`` is absent the user's personal OneDrive is targeted;
    when set (e.g. a team's SharePoint document-library drive id) the
    ``/drives/{id}`` base is used instead.
    Returns the slimmed leaf-folder driveItem. Raises ``OneDriveError`` on
    non-409 failures; 409 (already exists) is treated as success after a
    follow-up GET so the next iteration knows the existing folder's id.
    """
    parts = [p for p in folder_path.strip("/").split("/") if p]
    if not parts:
        raise OneDriveError("folder_path is empty")

    base = _drive_base(drive_id)
    parent_endpoint = f"{base}/root"
    last_item: dict[str, Any] = {}
    walked = ""
    for segment in parts:
        walked = f"{walked}/{segment}" if walked else segment
        body = {
            "name": segment,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }
        try:
            last_item = await graph_post(access_token, f"{parent_endpoint}/children", body)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise OneDriveError(
                    f"Failed to create folder /{walked}: HTTP {exc.response.status_code}",
                    status_code=exc.response.status_code,
                ) from exc
            # Folder already exists — re-fetch via path lookup so we have
            # the canonical id for the next iteration.
            walked_norm = _normalise_folder_path(walked)
            last_item = await graph_get(access_token, f"{base}/root:/{walked_norm}")

        parent_id = last_item.get("id")
        if not parent_id:
            raise OneDriveError(f"Folder /{walked} has no id after upsert")
        # Use the item id (not the drive-root path) for subsequent segments —
        # this works on both personal OneDrive and SharePoint drives.
        parent_endpoint = (
            f"/drives/{last_item.get('parentReference', {}).get('driveId') or drive_id or 'me'}/items/{parent_id}"
            if drive_id
            else f"/me/drive/items/{parent_id}"
        )

    return _slim_drive_item(last_item)


async def upload_file_to_drive(
    access_token: str,
    folder_path: str,
    filename: str,
    content: bytes,
    mime: str,
    *,
    conflict_behavior: str = "rename",
    drive_id: str | None = None,
) -> dict[str, Any]:
    """Upload ``content`` as ``folder_path/filename`` on the target drive.

    When ``drive_id`` is absent the user's personal OneDrive is targeted;
    when set (e.g. a team's SharePoint document-library drive id) the
    ``/drives/{id}`` base is used instead.
    Files <4 MiB use the simple PUT endpoint. Larger files use a Graph
    upload session with chunked uploads.

    Returns the slimmed driveItem of the new file. Raises ``OneDriveError``
    on non-success.
    """
    safe_name = sanitize_filename(filename)

    if len(content) <= _SIMPLE_UPLOAD_LIMIT:
        path = _build_simple_upload_path(
            folder_path, safe_name, conflict_behavior=conflict_behavior, drive_id=drive_id
        )
        resp = await graph_put_raw(access_token, path, content, mime or "application/octet-stream")
        if not resp.ok:
            raise OneDriveError(
                f"Simple upload failed: HTTP {resp.status_code} {resp.text[:300]}",
                status_code=resp.status_code,
            )
        return _slim_drive_item(resp.json())

    # >4 MiB — upload session.
    base = _drive_base(drive_id)
    folder_norm = _normalise_folder_path(folder_path)
    file_norm = urllib.parse.quote(safe_name, safe="")
    if folder_norm:
        session_endpoint = f"{base}/root:/{folder_norm}/{file_norm}:/createUploadSession"
    else:
        session_endpoint = f"{base}/root:/{file_norm}:/createUploadSession"

    session = await graph_post(
        access_token,
        session_endpoint,
        {
            "item": {
                "@microsoft.graph.conflictBehavior": conflict_behavior,
                "name": safe_name,
            }
        },
    )
    upload_url = session.get("uploadUrl")
    if not upload_url:
        raise OneDriveError("createUploadSession returned no uploadUrl")

    # Deliberately NOT routed through client.py. The upload session URL is a
    # pre-signed, short-lived URL on a different host, and Graph requires that
    # NO Authorization header is sent to it — so it is not a Graph API call and
    # none of the client helpers apply.
    async with httpx.AsyncClient(
        verify=not get_config().disable_ssl_verify, timeout=_TIMEOUT
    ) as client:
        total = len(content)
        offset = 0
        last_resp: httpx.Response | None = None
        while offset < total:
            chunk = content[offset : offset + _CHUNK_SIZE]
            end = offset + len(chunk) - 1
            resp = await client.put(
                upload_url,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{total}",
                },
                content=chunk,
            )
            if resp.status_code not in (200, 201, 202):
                raise OneDriveError(
                    f"Upload session chunk failed at byte {offset}: HTTP {resp.status_code}",
                    status_code=resp.status_code,
                )
            last_resp = resp
            offset += len(chunk)

        if last_resp is None:
            raise OneDriveError("Upload session produced no responses")
        return _slim_drive_item(last_resp.json())


async def update_drive_item_content(
    access_token: str,
    drive_id: str,
    item_id: str,
    content: bytes,
    mime: str,
    *,
    etag: str | None = None,
) -> dict[str, Any]:
    """Overwrite the content of an existing driveItem.

    Used by the in-session auto-sync flow: the first save returns the
    item id, subsequent edits PATCH the same item so the OneDrive file
    keeps the same URL/permissions.

    When ``etag`` is provided it's sent as ``If-Match`` — Graph returns
    412 if someone edited the file out-of-band. Translated into a typed
    ``OneDriveConflictError`` so the UI can offer a force-overwrite
    rather than silently clobbering the remote edit.
    """
    if not drive_id or not item_id:
        raise OneDriveError("drive_id and item_id are required")

    resp = await graph_put_raw(
        access_token,
        f"/drives/{drive_id}/items/{item_id}/content",
        content,
        mime or "application/octet-stream",
        extra_headers={"If-Match": etag} if etag else None,
    )
    if resp.status_code == 412:
        raise OneDriveConflictError()
    if not resp.ok:
        raise OneDriveError(
            f"Item update failed: HTTP {resp.status_code} {resp.text[:300]}",
            status_code=resp.status_code,
        )
    return _slim_drive_item(resp.json())


# ── Agent-facing tools ────────────────────────────────────────────────────────
# The helpers above have been driven by the internal tier since this package was
# extracted. These wrappers put a curated subset on the agent surface, in the
# write tier. The argument shapes deliberately differ from the internal tools:
# an LLM can reason about "Reports/Q3" and "summary.md", not about an opaque
# driveId/itemId pair it has to have fetched first.


class UploadFileInput(BaseModel):
    """Create a new file in the signed-in user's OneDrive."""

    folder_path: str = Field(
        default="",
        description="Folder path relative to the drive root, e.g. 'Reports/Q3'. Empty means the root.",
    )
    filename: str = Field(description="File name including extension, e.g. 'summary.md'")
    content: str = Field(description="Text content of the file (UTF-8)")
    mime_type: str = Field(
        default="text/plain",
        description="MIME type, e.g. text/plain, text/markdown, text/csv, text/html",
    )
    overwrite: bool = Field(
        default=False,
        description="True replaces an existing file of the same name; False keeps both by renaming.",
    )


class UpdateFileContentInput(BaseModel):
    """Replace the contents of an existing OneDrive file, keeping its id and URL."""

    item_id: str = Field(description="The driveItem id of the file to overwrite")
    content: str = Field(description="New text content of the file (UTF-8)")
    mime_type: str = Field(default="text/plain", description="MIME type of the new content")
    drive_id: str = Field(
        default="",
        description="Drive id. Leave empty for the signed-in user's own OneDrive.",
    )
    etag: str = Field(
        default="",
        description=(
            "Optional etag from a previous read. When supplied the update is refused if the "
            "file changed in the meantime, instead of silently overwriting someone else's edit."
        ),
    )


class CreateFolderInput(BaseModel):
    """Create a folder path in the signed-in user's OneDrive, including parents."""

    folder_path: str = Field(
        description="Folder path relative to the drive root, e.g. 'Reports/Q3'"
    )


class CreateSharingLinkInput(BaseModel):
    """Create a shareable link to a OneDrive or SharePoint file."""

    item_id: str = Field(description="The driveItem id to share")
    link_type: str = Field(
        default="view",
        description="'view' for read-only, 'edit' for read-write",
    )
    scope: str = Field(
        default="organization",
        description=(
            "'organization' for anyone signed in to the tenant, 'anonymous' for anyone with the "
            "link. Prefer 'organization' — 'anonymous' may be blocked by tenant policy."
        ),
    )
    drive_id: str = Field(
        default="", description="Drive id. Leave empty for the signed-in user's own OneDrive."
    )


@tool(
    description=(
        "Create a new text file in the signed-in user's OneDrive and return its id, name and "
        "web URL. Takes a folder path relative to the drive root and a filename. Use for saving "
        "notes, summaries, markdown or CSV. Text only — binary uploads are not supported. Set "
        "overwrite to replace a file of the same name rather than keeping both. Requires "
        "Files.ReadWrite."
    ),
    annotations=WRITE_CREATE,
    aliases=("upload_file",),
)
async def files_upload(params: UploadFileInput, context: dict) -> dict:
    token = context["access_token"]
    try:
        return await upload_file_to_drive(
            token,
            params.folder_path,
            sanitize_filename(params.filename),
            params.content.encode("utf-8"),
            params.mime_type,
            conflict_behavior="replace" if params.overwrite else "rename",
        )
    except OneDriveError as exc:
        return {"error": "upload_failed", "message": str(exc)}


@tool(
    description=(
        "Replace the contents of an existing OneDrive file, keeping its id, URL and sharing "
        "permissions intact. Takes an item id from files_search or files_list_recent. Pass the "
        "etag from a previous read to be refused rather than silently overwriting an edit "
        "someone else made in the meantime. Requires Files.ReadWrite."
    ),
    annotations=WRITE_UPDATE,
    aliases=("update_file_content",),
)
async def files_update_content(params: UpdateFileContentInput, context: dict) -> dict:
    token = context["access_token"]
    drive_id = params.drive_id
    if not drive_id:
        # update_drive_item_content addresses /drives/{id} explicitly, so resolve
        # the caller's own drive rather than making the model supply an id it has
        # no way to know.
        drive = await graph_get(token, "/me/drive", **{"$select": "id"})
        drive_id = drive.get("id", "")
        if not drive_id:
            return {"error": "drive_not_found", "message": "Could not resolve the user's OneDrive."}
    try:
        return await update_drive_item_content(
            token,
            drive_id,
            params.item_id,
            params.content.encode("utf-8"),
            params.mime_type,
            etag=params.etag or None,
        )
    except OneDriveConflictError as exc:
        return {
            "error": "file_changed",
            "message": (
                f"{exc} The file was modified since the etag you supplied. Re-read it, merge, "
                "and retry — or omit the etag to overwrite deliberately."
            ),
        }
    except OneDriveError as exc:
        return {"error": "update_failed", "message": str(exc)}


@tool(
    description=(
        "Create a folder in the signed-in user's OneDrive, creating any missing parent folders "
        "along the way. Takes a path relative to the drive root such as 'Reports/Q3' and "
        "returns the folder id and web URL. Succeeds quietly if the folder already exists, so "
        "it is safe to call before an upload. Requires Files.ReadWrite."
    ),
    annotations=WRITE_CREATE,
    aliases=("create_folder",),
)
async def files_create_folder(params: CreateFolderInput, context: dict) -> dict:
    token = context["access_token"]
    try:
        return await ensure_folder_exists(token, params.folder_path)
    except OneDriveError as exc:
        return {"error": "create_folder_failed", "message": str(exc)}


@tool(
    description=(
        "Create a shareable link to a OneDrive or SharePoint file and return the URL. Defaults "
        "to a read-only link scoped to the organisation; pass edit for write access, or "
        "anonymous for anyone with the link. Tenant policy commonly blocks anonymous links, in "
        "which case the tool says so. Requires Files.ReadWrite.All."
    ),
    annotations=WRITE_CREATE,
    aliases=("create_sharing_link",),
)
async def files_create_sharing_link(params: CreateSharingLinkInput, context: dict) -> dict:
    token = context["access_token"]
    if params.link_type not in {"view", "edit"}:
        return {
            "error": "invalid_arguments",
            "message": f"link_type must be 'view' or 'edit', got {params.link_type!r}",
        }
    if params.scope not in {"organization", "anonymous"}:
        return {
            "error": "invalid_arguments",
            "message": f"scope must be 'organization' or 'anonymous', got {params.scope!r}",
        }
    item_id = validate_graph_id(params.item_id, "item_id")
    base = _drive_base(validate_graph_id(params.drive_id, "drive_id") if params.drive_id else None)
    try:
        data = await graph_post(
            token,
            f"{base}/items/{item_id}/createLink",
            {"type": params.link_type, "scope": params.scope},
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 403:
            return {
                "error": "sharing_forbidden",
                "message": (
                    "Tenant policy refused this sharing link. Anonymous links are commonly "
                    "disabled — try scope='organization'."
                ),
            }
        return {"error": "create_link_failed", "message": f"Graph returned HTTP {status}"}
    link = data.get("link") or {}
    return {
        "url": link.get("webUrl", ""),
        "type": link.get("type", params.link_type),
        "scope": link.get("scope", params.scope),
        "item_id": item_id,
    }
