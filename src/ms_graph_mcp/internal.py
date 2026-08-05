"""Internal (deterministic) tool tier for graph-mcp.

These tools are for an embedding application's **own** ETL / workers / REST routes /
action-sinks calling the MCP as plain functions over HTTP — NOT the LLM-agent
surface and NOT external MCP clients. They are listed/dispatched only when the
request carries the internal scope (``X-Internal-Scope: true``), which the auth
middleware honours ONLY for the machine-secret principal (see ``auth.py``). They
are deliberately excluded from ``READ_TOOL_NAMES`` / ``WRITE_TOOL_NAMES`` so the
agent ``tools/list`` never advertises them.

``graph_request`` is the narrow, gated **passthrough** for the genuinely
arbitrary-path callers (the legacy ``graph_routes`` proxy, ``ba_buddy_discovery``,
``debug_routes``, ``@odata.nextLink`` pagination) — paths that don't map to a
fixed tool. Named internal tools for the common reusable operations are added
alongside it.
"""

from __future__ import annotations

import base64
from typing import Any

from pydantic import BaseModel, Field

from ms_graph_mcp.client import (
    graph_delete,
    graph_get,
    graph_get_url,
    graph_patch,
    graph_post,
)
from ms_graph_mcp.client import graph_probe_status as _graph_probe_status
from ms_graph_mcp.config import get_config
from ms_graph_mcp.email import fetch_message_attachments as _fetch_message_attachments
from ms_graph_mcp.files import files_get_group_drive as _get_group_drive
from ms_graph_mcp.files import walk_drive_descendants as _walk_drive_descendants
from ms_graph_mcp.files_write import ensure_folder_exists as _ensure_folder_exists
from ms_graph_mcp.files_write import update_drive_item_content as _update_drive_item_content
from ms_graph_mcp.files_write import upload_file_to_drive as _upload_file_to_drive
from ms_graph_mcp.tooling import tool


class GraphRequestInput(BaseModel):
    """Arbitrary Graph request. ``path`` is a Graph path (``/me/messages``) or a
    full ``@odata.nextLink`` URL (only with ``GET``)."""

    method: str = Field(default="GET", description="HTTP method: GET / POST / PATCH / DELETE")
    path: str = Field(
        description="Graph path (e.g. /me/drive/items/{id}/children) or full nextLink URL"
    )
    params: dict[str, Any] | None = Field(default=None, description="Query params for GET")
    body: dict[str, Any] | None = Field(default=None, description="JSON body for POST/PATCH")


@tool(
    description=(
        "INTERNAL passthrough — issue an arbitrary Microsoft Graph request. For the "
        "Trusted first-party ETL/REST callers only; not part of the agent tool surface."
    )
)
async def graph_request(params: GraphRequestInput, context: dict) -> Any:
    """Dispatch an arbitrary Graph call through the canonical client.

    Runs on ``context["access_token"]`` — for per-user callers the dispatch layer
    has already OBO-exchanged the inbound user assertion into a Graph token.
    """
    token = context["access_token"]
    method = (params.method or "GET").upper()

    # A full nextLink URL bakes the page cursor into the URL — GET only.
    if params.path.startswith("http"):
        if method != "GET":
            raise ValueError("graph_request: full URLs (nextLink) support GET only")
        return await graph_get_url(token, params.path)

    if method == "GET":
        return await graph_get(token, params.path, **(params.params or {}))
    if method == "POST":
        return await graph_post(token, params.path, params.body or {})
    if method == "PATCH":
        return await graph_patch(token, params.path, params.body or {})
    if method == "DELETE":
        await graph_delete(token, params.path)
        return {"ok": True}
    raise ValueError(f"graph_request: unsupported method {method!r}")


# ── Named internal tools ──────────────────────────────────────────────────────
# Reusable Graph operations first-party ETL/workers call as plain functions over
# HTTP. Each wraps the canonical domain helper; the Graph token comes from context
# (already OBO-exchanged by dispatch for per-user callers).


class WalkDriveDescendantsInput(BaseModel):
    """BFS-walk a drive folder and return its descendant **file** items."""

    drive_id: str = Field(description="OneDrive/SharePoint drive id")
    item_id: str = Field(description="Folder item id ('root' targets the user's drive root)")
    max_files: int | None = Field(
        default=None, description="Cap on returned files (defaults to the server's browse limit)"
    )


@tool(
    description=(
        "INTERNAL — recursively walk a drive folder server-side and return its "
        "descendant file items as a list. Folders are traversed but not returned. "
        "Collapses the recursion + pagination into one call (the passthrough can't)."
    )
)
async def graph_walk_drive_descendants(
    params: WalkDriveDescendantsInput, context: dict
) -> list[dict]:
    token = context["access_token"]
    return [
        item
        async for item in _walk_drive_descendants(
            token, params.drive_id, params.item_id, max_files=params.max_files
        )
    ]


class EnsureDriveFolderInput(BaseModel):
    """Create a root-relative folder path on a drive if missing."""

    folder_path: str = Field(description="Forward-slash separated, root-relative path")
    drive_id: str | None = Field(
        default=None,
        description="Target drive id. Absent → personal OneDrive; set → that drive (e.g. team SharePoint library).",
    )


@tool(
    description=(
        "INTERNAL — ensure a root-relative folder path exists on a drive, "
        "creating any missing intermediates. Absent drive_id → personal OneDrive; "
        "set drive_id → that drive (e.g. team SharePoint library). "
        "Returns the slimmed leaf-folder item."
    )
)
async def graph_ensure_drive_folder(params: EnsureDriveFolderInput, context: dict) -> dict:
    return await _ensure_folder_exists(
        context["access_token"], params.folder_path, drive_id=params.drive_id
    )


class UploadFileToDriveInput(BaseModel):
    """Upload a file to a drive. Bytes are carried base64-encoded."""

    folder_path: str = Field(description="Root-relative destination folder")
    filename: str = Field(description="File name (sanitised server-side)")
    content_b64: str = Field(description="Base64-encoded file content")
    mime: str = Field(description="MIME type")
    conflict_behavior: str = Field(
        default="rename", description="Graph conflictBehavior: rename / replace / fail"
    )
    drive_id: str | None = Field(
        default=None,
        description="Target drive id. Absent → personal OneDrive; set → that drive (e.g. team SharePoint library).",
    )


@tool(
    description=(
        "INTERNAL — upload a file to a drive (content base64-encoded). "
        "Absent drive_id → personal OneDrive; set drive_id → that drive. "
        "Handles simple + chunked upload server-side. Returns the slimmed driveItem."
    )
)
async def graph_upload_file_to_drive(params: UploadFileToDriveInput, context: dict) -> dict:
    content = base64.b64decode(params.content_b64)
    return await _upload_file_to_drive(
        context["access_token"],
        params.folder_path,
        params.filename,
        content,
        params.mime,
        conflict_behavior=params.conflict_behavior,
        drive_id=params.drive_id,
    )


class GetGroupDriveInput(BaseModel):
    """Resolve a Microsoft 365 group (Team) to its SharePoint document-library drive."""

    group_id: str = Field(description="Microsoft 365 group id — same as Teams team.id")


@tool(
    description=(
        "INTERNAL — resolve a Microsoft 365 group (Team) to its default SharePoint "
        "document-library drive. Returns {drive_id, name, web_url, drive_type}. "
        "Use this before graph_ensure_drive_folder / graph_upload_file_to_drive to "
        "obtain the drive_id for a team save."
    )
)
async def graph_get_group_drive(params: GetGroupDriveInput, context: dict) -> dict:
    from ms_graph_mcp.files import GetGroupDriveInput as _FilesInput

    return await _get_group_drive(_FilesInput(group_id=params.group_id), context)


class DownloadDriveItemInput(BaseModel):
    drive_id: str = Field(
        description="Drive id (from get_group_drive or the user's personal drive id)"
    )
    item_id: str = Field(description="DriveItem id to download")
    max_bytes: int = Field(
        default=5 * 1024 * 1024, description="Safety cap on download size (default 5 MiB)"
    )


@tool(
    description=(
        "INTERNAL — download a DriveItem's raw bytes and return them base64-encoded. "
        "Also returns item metadata (name, mimeType, etag, size, web_url). "
        "Used by the open-from-drive endpoint to fetch a file server-side without "
        "exposing the Graph token to the browser. Capped at max_bytes (default 5 MiB)."
    )
)
async def graph_download_drive_item(params: DownloadDriveItemInput, context: dict) -> dict:
    import httpx

    token = context["access_token"]

    # Fetch metadata — includes @microsoft.graph.downloadUrl (pre-authorised, no token needed)
    meta = await graph_get(
        token,
        f"/drives/{params.drive_id}/items/{params.item_id}",
    )
    download_url: str = meta.get("@microsoft.graph.downloadUrl", "")
    name: str = meta.get("name", "")
    etag: str = (meta.get("eTag") or "").strip('"')
    size: int = meta.get("size") or 0
    web_url: str = meta.get("webUrl", "")
    file_info: dict = meta.get("file", {})
    mime_type: str = file_info.get("mimeType", "application/octet-stream")

    if not download_url:
        raise ValueError(f"No downloadUrl for item {params.item_id} — item may be a folder")

    if size > params.max_bytes:
        raise ValueError(f"File is {size} bytes, exceeds the {params.max_bytes}-byte safety cap")

    # Download via the pre-authorised URL (no auth header required)
    async with httpx.AsyncClient(
        verify=not get_config().disable_ssl_verify,
        timeout=httpx.Timeout(60.0),
        follow_redirects=True,
    ) as client:
        resp = await client.get(download_url)
        resp.raise_for_status()
        raw = resp.content

    return {
        "content_b64": base64.b64encode(raw).decode("ascii"),
        "mime_type": mime_type,
        "name": name,
        "etag": etag,
        "size": len(raw),
        "drive_id": params.drive_id,
        "item_id": params.item_id,
        "web_url": web_url,
    }


# ── App-only + binary internal tools ──────────────────────────────────────────
# Cover the ETL/worker callers the passthrough can't model: the app-only access
# probe (no user token), binary message-attachment fetch (base64 out), and the
# existing-item content overwrite (base64 in).


class ProbeGraphAccessInput(BaseModel):
    """App-only access-revalidation probe — GET a path, return the HTTP status."""

    path: str = Field(description="Graph path to probe (e.g. /users/{id}/messages/{mid})")
    timeout_seconds: float = Field(default=10.0, description="HTTP timeout")


@tool(
    description=(
        "INTERNAL app-only — GET a Graph path and return its HTTP status code "
        "without raising (RBAC revalidation). Runs on a client-credentials token."
    )
)
async def probe_graph_access(params: ProbeGraphAccessInput, context: dict) -> dict:
    status = await _graph_probe_status(
        context["access_token"], params.path, timeout_seconds=params.timeout_seconds
    )
    return {"status": status}


class FetchMessageAttachmentsInput(BaseModel):
    """List + download a message's non-inline file attachments (bytes base64)."""

    message_id: str = Field(description="Graph message id")
    max_mb: int = Field(default=25, description="Per-attachment size cap (MB)")


@tool(
    description=(
        "INTERNAL — list + download a message's non-inline file attachments. "
        "Each item is {id, name, contentType, size, content_b64} (base64 bytes)."
    )
)
async def fetch_message_attachments(
    params: FetchMessageAttachmentsInput, context: dict
) -> list[dict]:
    items = await _fetch_message_attachments(
        context["access_token"], params.message_id, max_mb=params.max_mb
    )
    out: list[dict] = []
    for item in items:
        raw = item.get("content_bytes")
        slim = {k: v for k, v in item.items() if k != "content_bytes"}
        if isinstance(raw, (bytes, bytearray)):
            slim["content_b64"] = base64.b64encode(bytes(raw)).decode("ascii")
        out.append(slim)
    return out


class UpdateDriveItemContentInput(BaseModel):
    """Overwrite an existing driveItem's content (bytes base64)."""

    drive_id: str = Field(description="Drive id")
    item_id: str = Field(description="Drive item id")
    content_b64: str = Field(description="Base64-encoded new content")
    mime: str = Field(description="MIME type")
    etag: str | None = Field(default=None, description="If-Match etag for optimistic concurrency")


@tool(
    description=(
        "INTERNAL — overwrite the content of an existing OneDrive item (content "
        "base64-encoded). Returns the slimmed driveItem."
    )
)
async def graph_update_drive_item_content(
    params: UpdateDriveItemContentInput, context: dict
) -> dict:
    content = base64.b64decode(params.content_b64)
    return await _update_drive_item_content(
        context["access_token"],
        params.drive_id,
        params.item_id,
        content,
        params.mime,
        etag=params.etag,
    )
