"""Internal (deterministic) tool tier — scope gating + passthrough + client-creds.

The internal tier is for the host application's own ETL/REST callers (machine
principal + X-Internal-Scope). It must be invisible to agents/external clients and
rejected without the internal scope.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ms_graph_mcp import obo
from ms_graph_mcp.allowlists import INTERNAL_TOOL_NAME_SET, READ_TOOL_NAME_SET, WRITE_TOOL_NAME_SET
from ms_graph_mcp.context import current_request_context
from ms_graph_mcp.internal import (
    EnsureDriveFolderInput,
    GraphRequestInput,
    UploadFileToDriveInput,
    WalkDriveDescendantsInput,
    graph_ensure_drive_folder,
    graph_request,
    graph_upload_file_to_drive,
    graph_walk_drive_descendants,
)
from ms_graph_mcp.server import dispatch_graph_tool, list_graph_tools

# ── allowlist hygiene ─────────────────────────────────────────────────────────


def test_internal_tools_never_in_agent_allowlists():
    assert INTERNAL_TOOL_NAME_SET
    assert not (INTERNAL_TOOL_NAME_SET & READ_TOOL_NAME_SET)
    assert not (INTERNAL_TOOL_NAME_SET & WRITE_TOOL_NAME_SET)


# ── tools/list gating ───────────────────────────────────────────────────────


async def test_internal_tools_hidden_from_agent_list():
    cv = current_request_context.set({"access_token": "t", "write_scope": True})
    try:
        names = {t.name for t in await list_graph_tools()}
    finally:
        current_request_context.reset(cv)
    # Read + write present (write_scope on), internal absent.
    assert "graph_request" not in names
    assert READ_TOOL_NAME_SET <= names


async def test_internal_tools_listed_only_with_internal_scope():
    cv = current_request_context.set({"access_token": "t", "internal_scope": True})
    try:
        names = {t.name for t in await list_graph_tools()}
    finally:
        current_request_context.reset(cv)
    assert "graph_request" in names


# ── dispatch gating ──────────────────────────────────────────────────────────


async def test_dispatch_rejects_internal_tool_without_scope():
    cv = current_request_context.set({"access_token": "t"})  # no internal_scope
    try:
        result = await dispatch_graph_tool("graph_request", {"method": "GET", "path": "/me"})
    finally:
        current_request_context.reset(cv)
    assert json.loads(result[0].text)["error"] == "internal_scope_required"


async def test_dispatch_allows_internal_tool_with_scope(monkeypatch):
    captured: dict = {}

    class _Reg:
        async def call(self, name, arguments_json, context):
            captured["name"] = name
            return {"ok": True}

    monkeypatch.setattr("ms_graph_mcp.server.get_registry", lambda: _Reg())
    cv = current_request_context.set(
        {"access_token": "user-tok", "internal_scope": True, "user_email": "u@x"}
    )
    try:
        result = await dispatch_graph_tool("graph_request", {"method": "GET", "path": "/me"})
    finally:
        current_request_context.reset(cv)
    assert captured["name"] == "graph_request"
    assert json.loads(result[0].text) == {"ok": True}


# ── graph_request passthrough behaviour ───────────────────────────────────────


async def test_graph_request_routes_get_through_client(monkeypatch):
    fake_get = AsyncMock(return_value={"value": [1]})
    monkeypatch.setattr("ms_graph_mcp.internal.graph_get", fake_get)
    out = await graph_request(
        GraphRequestInput(method="GET", path="/me/messages", params={"$top": 5}),
        {"access_token": "g-tok"},
    )
    assert out == {"value": [1]}
    assert fake_get.await_args.args[0] == "g-tok"
    assert fake_get.await_args.args[1] == "/me/messages"


async def test_graph_request_full_url_uses_get_url(monkeypatch):
    fake_url = AsyncMock(return_value={"value": ["page2"]})
    monkeypatch.setattr("ms_graph_mcp.internal.graph_get_url", fake_url)
    out = await graph_request(
        GraphRequestInput(method="GET", path="https://graph.microsoft.com/v1.0/x?$skip=2"),
        {"access_token": "g-tok"},
    )
    assert out == {"value": ["page2"]}


async def test_graph_request_full_url_rejects_non_get():
    with pytest.raises(ValueError, match="GET only"):
        await graph_request(
            GraphRequestInput(method="POST", path="https://graph.microsoft.com/v1.0/x"),
            {"access_token": "g-tok"},
        )


# ── client-credentials (app-only) helper ──────────────────────────────────────


async def test_acquire_token_for_client_returns_token(monkeypatch):
    app = MagicMock()
    app.acquire_token_for_client = MagicMock(return_value={"access_token": "app-tok"})
    monkeypatch.setattr(obo, "_get_app", lambda *a, **k: app)
    out = await obo.acquire_token_for_client(
        ["https://graph.microsoft.com/.default"], tenant_id="t", client_id="c", client_secret="s"
    )
    assert out == "app-tok"


async def test_acquire_token_for_client_raises_on_rejection(monkeypatch):
    app = MagicMock()
    app.acquire_token_for_client = MagicMock(return_value={"error": "invalid_client"})
    monkeypatch.setattr(obo, "_get_app", lambda *a, **k: app)
    with pytest.raises(obo.OboError, match="invalid_client"):
        await obo.acquire_token_for_client(["s"], tenant_id="t", client_id="c", client_secret="x")


# ── Named internal tools ──────────────────────────────────────────────────────


def test_walk_drive_descendants_in_internal_allowlist_only():
    assert "graph_walk_drive_descendants" in INTERNAL_TOOL_NAME_SET
    assert "graph_walk_drive_descendants" not in READ_TOOL_NAME_SET
    assert "graph_walk_drive_descendants" not in WRITE_TOOL_NAME_SET


async def test_walk_drive_descendants_listed_only_with_internal_scope():
    cv = current_request_context.set({"access_token": "t", "internal_scope": True})
    try:
        names = {t.name for t in await list_graph_tools()}
    finally:
        current_request_context.reset(cv)
    assert "graph_walk_drive_descendants" in names


async def test_walk_drive_descendants_hidden_from_agent_list():
    cv = current_request_context.set({"access_token": "t", "write_scope": True})
    try:
        names = {t.name for t in await list_graph_tools()}
    finally:
        current_request_context.reset(cv)
    assert "graph_walk_drive_descendants" not in names


async def test_walk_drive_descendants_collects_generator(monkeypatch):
    async def _gen(token, drive_id, item_id, *, max_files=None):
        assert token == "g-tok"
        assert drive_id == "drv"
        assert item_id == "root"
        assert max_files == 10
        for i in (1, 2, 3):
            yield {"id": str(i), "file": {}}

    monkeypatch.setattr("ms_graph_mcp.internal._walk_drive_descendants", _gen)
    out = await graph_walk_drive_descendants(
        WalkDriveDescendantsInput(drive_id="drv", item_id="root", max_files=10),
        {"access_token": "g-tok"},
    )
    assert [f["id"] for f in out] == ["1", "2", "3"]


def test_drive_write_tools_in_internal_allowlist_only():
    for name in ("graph_ensure_drive_folder", "graph_upload_file_to_drive"):
        assert name in INTERNAL_TOOL_NAME_SET
        assert name not in READ_TOOL_NAME_SET
        assert name not in WRITE_TOOL_NAME_SET


async def test_ensure_drive_folder_routes_through_helper(monkeypatch):
    fake = AsyncMock(return_value={"id": "f1", "name": "Docs"})
    monkeypatch.setattr("ms_graph_mcp.internal._ensure_folder_exists", fake)
    out = await graph_ensure_drive_folder(
        EnsureDriveFolderInput(folder_path="Reports/Docs"), {"access_token": "g-tok"}
    )
    assert out == {"id": "f1", "name": "Docs"}
    assert fake.await_args.args == ("g-tok", "Reports/Docs")


async def test_upload_file_decodes_base64_and_routes(monkeypatch):
    fake = AsyncMock(return_value={"id": "u1", "name": "a.txt"})
    monkeypatch.setattr("ms_graph_mcp.internal._upload_file_to_drive", fake)
    raw = b"hello bytes"
    out = await graph_upload_file_to_drive(
        UploadFileToDriveInput(
            folder_path="Reports",
            filename="a.txt",
            content_b64=base64.b64encode(raw).decode(),
            mime="text/plain",
        ),
        {"access_token": "g-tok"},
    )
    assert out == {"id": "u1", "name": "a.txt"}
    # decoded bytes reach the helper, not the b64 string
    assert fake.await_args.args[3] == raw
    assert fake.await_args.args[0] == "g-tok"
    assert fake.await_args.kwargs["conflict_behavior"] == "rename"


# ── App-only + binary tools ───────────────────────────────────────────────────


async def test_fetch_message_attachments_base64_encodes_bytes(monkeypatch):
    from ms_graph_mcp.internal import FetchMessageAttachmentsInput, fetch_message_attachments

    fake = AsyncMock(
        return_value=[
            {
                "id": "a1",
                "name": "f.pdf",
                "contentType": "application/pdf",
                "size": 3,
                "content_bytes": b"abc",
            }
        ]
    )
    monkeypatch.setattr("ms_graph_mcp.internal._fetch_message_attachments", fake)
    out = await fetch_message_attachments(
        FetchMessageAttachmentsInput(message_id="m1"), {"access_token": "g-tok"}
    )
    assert out[0]["name"] == "f.pdf"
    assert "content_bytes" not in out[0]
    assert base64.b64decode(out[0]["content_b64"]) == b"abc"


async def test_update_drive_item_content_decodes_base64(monkeypatch):
    from ms_graph_mcp.internal import UpdateDriveItemContentInput, graph_update_drive_item_content

    fake = AsyncMock(return_value={"id": "i1"})
    monkeypatch.setattr("ms_graph_mcp.internal._update_drive_item_content", fake)
    raw = b"new-content"
    out = await graph_update_drive_item_content(
        UpdateDriveItemContentInput(
            drive_id="d", item_id="i", content_b64=base64.b64encode(raw).decode(), mime="text/plain"
        ),
        {"access_token": "g-tok"},
    )
    assert out == {"id": "i1"}
    assert fake.await_args.args[3] == raw  # decoded bytes


# ── get_group_drive (agent-tier) + graph_get_group_drive (internal-tier) ─────


def test_get_group_drive_in_read_allowlist():
    assert "get_group_drive" in READ_TOOL_NAME_SET
    assert "get_group_drive" not in INTERNAL_TOOL_NAME_SET
    assert "get_group_drive" not in WRITE_TOOL_NAME_SET


def test_graph_get_group_drive_in_internal_allowlist_only():
    assert "graph_get_group_drive" in INTERNAL_TOOL_NAME_SET
    assert "graph_get_group_drive" not in READ_TOOL_NAME_SET
    assert "graph_get_group_drive" not in WRITE_TOOL_NAME_SET


async def test_get_group_drive_calls_graph_and_returns_drive(monkeypatch):
    from ms_graph_mcp.files import GetGroupDriveInput, get_group_drive

    fake_get = AsyncMock(
        return_value={
            "id": "drive-abc",
            "name": "Documents",
            "webUrl": "https://contoso.sharepoint.com/...",
            "driveType": "documentLibrary",
        }
    )
    monkeypatch.setattr("ms_graph_mcp.files.graph_get", fake_get)
    out = await get_group_drive(GetGroupDriveInput(group_id="group-123"), {"access_token": "tok"})
    assert out["drive_id"] == "drive-abc"
    assert out["name"] == "Documents"
    assert out["web_url"].startswith("https://")
    assert out["drive_type"] == "documentLibrary"
    # Graph path is /groups/{group_id}/drive
    call_path = fake_get.await_args.args[1]
    assert call_path == "/groups/group-123/drive"


async def test_graph_get_group_drive_delegates_to_agent_tool(monkeypatch):
    from ms_graph_mcp.internal import GetGroupDriveInput as InternalInput
    from ms_graph_mcp.internal import graph_get_group_drive

    fake_get = AsyncMock(
        return_value={
            "id": "drv-xyz",
            "name": "Docs",
            "webUrl": "https://x",
            "driveType": "documentLibrary",
        }
    )
    monkeypatch.setattr("ms_graph_mcp.files.graph_get", fake_get)
    out = await graph_get_group_drive(InternalInput(group_id="g-999"), {"access_token": "svc-tok"})
    assert out["drive_id"] == "drv-xyz"
    assert fake_get.await_args.args[1] == "/groups/g-999/drive"


async def test_app_only_dispatch_mints_cc_token_without_user_token(monkeypatch):
    """probe_graph_access is app-only: dispatch mints a client-credentials token
    and runs even with no access_token (user token) present."""
    captured: dict = {}

    class _Reg:
        async def call(self, name, arguments_json, context):
            captured["name"] = name
            captured["access_token"] = context.get("access_token")
            return {"status": 200}

    monkeypatch.setattr("ms_graph_mcp.server.get_registry", lambda: _Reg())
    monkeypatch.setattr(
        "ms_graph_mcp.obo.acquire_token_for_client", AsyncMock(return_value="cc-tok")
    )
    cv = current_request_context.set({"internal_scope": True})  # no access_token
    try:
        result = await dispatch_graph_tool("probe_graph_access", {"path": "/me"})
    finally:
        current_request_context.reset(cv)
    assert captured["name"] == "probe_graph_access"
    assert captured["access_token"] == "cc-tok"
    assert json.loads(result[0].text) == {"status": 200}
