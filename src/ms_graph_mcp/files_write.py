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

from ms_graph_mcp.client import graph_get, graph_post
from ms_graph_mcp.config import get_config

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


def _build_simple_upload_url(
    folder_path: str,
    filename: str,
    *,
    conflict_behavior: str = "rename",
    drive_id: str | None = None,
) -> str:
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
    return f"{_GRAPH_BASE}{endpoint}{suffix}"


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
        parent_endpoint = f"/drives/{last_item.get('parentReference', {}).get('driveId') or drive_id or 'me'}/items/{parent_id}" if drive_id else f"/me/drive/items/{parent_id}"

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
        url = _build_simple_upload_url(
            folder_path, safe_name, conflict_behavior=conflict_behavior, drive_id=drive_id
        )
        with _tracer.start_as_current_span(
            "graph.onedrive.upload",
            attributes={
                "graph.upload.size": len(content),
                "graph.upload.path": f"{folder_path}/{safe_name}",
            },
        ) as span:
            async with httpx.AsyncClient(
                verify=not get_config().disable_ssl_verify, timeout=_TIMEOUT
            ) as client:
                resp = await client.put(
                    url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": mime or "application/octet-stream",
                    },
                    content=content,
                )
                span.set_attribute("http.status_code", resp.status_code)
                if not resp.is_success:
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

    url = f"{_GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": mime or "application/octet-stream",
    }
    if etag:
        headers["If-Match"] = etag

    with _tracer.start_as_current_span(
        "graph.onedrive.update",
        attributes={
            "graph.upload.size": len(content),
            "graph.drive.item_id": item_id,
            "graph.upload.if_match": bool(etag),
        },
    ) as span:
        async with httpx.AsyncClient(
            verify=not get_config().disable_ssl_verify, timeout=_TIMEOUT
        ) as client:
            resp = await client.put(url, headers=headers, content=content)
            span.set_attribute("http.status_code", resp.status_code)
            if resp.status_code == 412:
                raise OneDriveConflictError()
            if not resp.is_success:
                raise OneDriveError(
                    f"Item update failed: HTTP {resp.status_code} {resp.text[:300]}",
                    status_code=resp.status_code,
                )
            return _slim_drive_item(resp.json())
