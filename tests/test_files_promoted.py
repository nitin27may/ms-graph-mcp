"""The OneDrive write tools promoted from the internal tier onto the agent surface.

The underlying Graph helpers (``upload_file_to_drive``, ``ensure_folder_exists``,
``update_drive_item_content``) already have their own coverage in
``test_files_write.py``. These tests cover what the promotion added: the tool
wrappers, the LLM-facing argument shapes, and — most importantly — that a Graph
failure comes back as a structured error the model can act on rather than an
exception that surfaces as a protocol fault.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ms_graph_mcp.context import current_request_context
from ms_graph_mcp.files_write import (
    CreateFolderInput,
    CreateSharingLinkInput,
    OneDriveConflictError,
    OneDriveError,
    UpdateFileContentInput,
    UploadFileInput,
    files_create_folder,
    files_create_sharing_link,
    files_update_content,
    files_upload,
)

_CTX = {"access_token": "tok"}


# ── files_upload ───────────────────────────────────────────────────────────────


async def test_upload_file_encodes_text_and_passes_through():
    with patch("ms_graph_mcp.files_write.upload_file_to_drive", new=AsyncMock()) as mock:
        mock.return_value = {"id": "i1", "name": "notes.md", "web_url": "https://x"}
        result = await files_upload(
            UploadFileInput(
                folder_path="Reports/Q3",
                filename="notes.md",
                content="# hi",
                mime_type="text/markdown",
            ),
            _CTX,
        )
    args, kwargs = mock.call_args
    assert args[1] == "Reports/Q3"
    assert args[2] == "notes.md"
    assert args[3] == b"# hi", "content must reach Graph as UTF-8 bytes"
    assert args[4] == "text/markdown"
    assert kwargs["conflict_behavior"] == "rename"
    assert result["id"] == "i1"


async def test_upload_file_overwrite_flag_maps_to_replace():
    """The model gets a boolean; Graph wants a conflictBehavior string."""
    with patch("ms_graph_mcp.files_write.upload_file_to_drive", new=AsyncMock()) as mock:
        mock.return_value = {}
        await files_upload(
            UploadFileInput(filename="a.txt", content="x", overwrite=True),
            _CTX,
        )
    assert mock.call_args.kwargs["conflict_behavior"] == "replace"


async def test_upload_file_sanitises_the_filename():
    """An LLM will happily propose a filename with a slash in it."""
    with patch("ms_graph_mcp.files_write.upload_file_to_drive", new=AsyncMock()) as mock:
        mock.return_value = {}
        await files_upload(
            UploadFileInput(filename="a/b:c*.txt", content="x"),
            _CTX,
        )
    sent = mock.call_args.args[2]
    assert "/" not in sent and ":" not in sent and "*" not in sent


async def test_upload_file_returns_structured_error_not_an_exception():
    with patch("ms_graph_mcp.files_write.upload_file_to_drive", new=AsyncMock()) as mock:
        mock.side_effect = OneDriveError("quota exceeded", status_code=507)
        result = await files_upload(UploadFileInput(filename="a.txt", content="x"), _CTX)
    assert result["error"] == "upload_failed"
    assert "quota exceeded" in result["message"]


# ── files_update_content ───────────────────────────────────────────────────────


async def test_update_file_content_resolves_the_users_drive_when_omitted():
    """The model has no way to know a driveId, so the tool resolves it."""
    with (
        patch("ms_graph_mcp.files_write.graph_get", new=AsyncMock()) as get,
        patch("ms_graph_mcp.files_write.update_drive_item_content", new=AsyncMock()) as upd,
    ):
        get.return_value = {"id": "drive-abc"}
        upd.return_value = {"id": "i1"}
        await files_update_content(
            UpdateFileContentInput(item_id="i1", content="new"),
            _CTX,
        )
    assert get.call_args.args[1] == "/me/drive"
    assert upd.call_args.args[1] == "drive-abc"
    assert upd.call_args.args[3] == b"new"


async def test_update_file_content_uses_supplied_drive_without_a_lookup():
    with (
        patch("ms_graph_mcp.files_write.graph_get", new=AsyncMock()) as get,
        patch("ms_graph_mcp.files_write.update_drive_item_content", new=AsyncMock()) as upd,
    ):
        upd.return_value = {}
        await files_update_content(
            UpdateFileContentInput(item_id="i1", content="x", drive_id="d9"),
            _CTX,
        )
    get.assert_not_called()
    assert upd.call_args.args[1] == "d9"


async def test_update_file_content_surfaces_a_conflict_with_recovery_advice():
    """412 means someone else edited it. The model needs to know how to recover."""
    with (
        patch("ms_graph_mcp.files_write.graph_get", new=AsyncMock(return_value={"id": "d"})),
        patch("ms_graph_mcp.files_write.update_drive_item_content", new=AsyncMock()) as upd,
    ):
        upd.side_effect = OneDriveConflictError()
        result = await files_update_content(
            UpdateFileContentInput(item_id="i1", content="x", etag='W/"1"'),
            _CTX,
        )
    assert result["error"] == "file_changed"
    assert "retry" in result["message"].lower()


async def test_update_file_content_passes_etag_as_none_when_blank():
    """Empty string is the model's way of omitting it; Graph wants no If-Match."""
    with (
        patch("ms_graph_mcp.files_write.graph_get", new=AsyncMock(return_value={"id": "d"})),
        patch("ms_graph_mcp.files_write.update_drive_item_content", new=AsyncMock()) as upd,
    ):
        upd.return_value = {}
        await files_update_content(UpdateFileContentInput(item_id="i1", content="x"), _CTX)
    assert upd.call_args.kwargs["etag"] is None


# ── files_create_folder ─────────────────────────────────────────────────────────────


async def test_create_folder_delegates_to_ensure_folder_exists():
    with patch("ms_graph_mcp.files_write.ensure_folder_exists", new=AsyncMock()) as mock:
        mock.return_value = {"id": "f1", "name": "Q3"}
        result = await files_create_folder(CreateFolderInput(folder_path="Reports/Q3"), _CTX)
    assert mock.call_args.args[1] == "Reports/Q3"
    assert result["id"] == "f1"


async def test_create_folder_returns_structured_error():
    with patch("ms_graph_mcp.files_write.ensure_folder_exists", new=AsyncMock()) as mock:
        mock.side_effect = OneDriveError("nope")
        result = await files_create_folder(CreateFolderInput(folder_path="x"), _CTX)
    assert result["error"] == "create_folder_failed"


# ── files_create_sharing_link ───────────────────────────────────────────────────────


async def test_create_sharing_link_defaults_to_organisation_view():
    with patch("ms_graph_mcp.files_write.graph_post", new=AsyncMock()) as post:
        post.return_value = {
            "link": {"webUrl": "https://share/x", "type": "view", "scope": "organization"}
        }
        result = await files_create_sharing_link(CreateSharingLinkInput(item_id="i1"), _CTX)
    body = post.call_args.args[2]
    assert body == {"type": "view", "scope": "organization"}
    assert post.call_args.args[1] == "/me/drive/items/i1/createLink"
    assert result["url"] == "https://share/x"


async def test_create_sharing_link_addresses_a_named_drive():
    with patch("ms_graph_mcp.files_write.graph_post", new=AsyncMock()) as post:
        post.return_value = {"link": {}}
        await files_create_sharing_link(
            CreateSharingLinkInput(item_id="i1", drive_id="d9", link_type="edit"), _CTX
        )
    assert post.call_args.args[1] == "/drives/d9/items/i1/createLink"
    assert post.call_args.args[2]["type"] == "edit"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"item_id": "i1", "link_type": "delete"},
        {"item_id": "i1", "scope": "everyone"},
    ],
)
async def test_create_sharing_link_rejects_bad_enums_before_calling_graph(kwargs):
    with patch("ms_graph_mcp.files_write.graph_post", new=AsyncMock()) as post:
        result = await files_create_sharing_link(CreateSharingLinkInput(**kwargs), _CTX)
    post.assert_not_called()
    assert result["error"] == "invalid_arguments"


async def test_create_sharing_link_explains_a_403_rather_than_leaking_http():
    """Tenants commonly disable anonymous links; the model should be told that."""
    response = httpx.Response(403, request=httpx.Request("POST", "https://graph.microsoft.com"))
    with patch("ms_graph_mcp.files_write.graph_post", new=AsyncMock()) as post:
        post.side_effect = httpx.HTTPStatusError(
            "forbidden", request=response.request, response=response
        )
        result = await files_create_sharing_link(
            CreateSharingLinkInput(item_id="i1", scope="anonymous"), _CTX
        )
    assert result["error"] == "sharing_forbidden"
    assert "organization" in result["message"]


async def test_create_sharing_link_rejects_an_injected_item_id():
    with patch("ms_graph_mcp.files_write.graph_post", new=AsyncMock()) as post:
        with pytest.raises(ValueError):
            await files_create_sharing_link(CreateSharingLinkInput(item_id="../../me/drive"), _CTX)
    post.assert_not_called()


# ── tier placement ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["files_upload", "files_update_content", "files_create_folder", "files_create_sharing_link"],
)
async def test_promoted_write_tools_are_refused_without_write_scope(name, call_tool):
    """Promotion must not have leaked a mutation onto the always-on read surface."""
    cv = current_request_context.set({"access_token": "tok", "write_scope": False})
    try:
        result = await call_tool(name, {})
    finally:
        current_request_context.reset(cv)
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == "write_scope_required"
