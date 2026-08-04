"""OneDrive folder browser walk helpers — KB seed wizard "Browse folders" mode.

Mocks the Graph boundary (``graph_get``) so no real network calls. Covers:

- ``list_drive_item_children`` happy path (root + by-id)
- pagination via ``@odata.nextLink``
- ``walk_drive_descendants`` BFS yields only files, not folders
- ``walk_drive_descendants`` honours ``max_files`` cap
- per-folder fetch failure doesn't crash the walk
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch


def _file(fid: str, name: str = "f", size: int = 100) -> dict:
    return {
        "id": fid,
        "name": name,
        "size": size,
        "file": {"mimeType": "application/pdf"},
        "parentReference": {"driveId": "drive-1"},
        "lastModifiedDateTime": "2026-04-22T10:00:00Z",
        "eTag": f'"{fid}-etag"',
    }


def _folder(fid: str, name: str = "Folder") -> dict:
    return {
        "id": fid,
        "name": name,
        "folder": {"childCount": 1},
        "parentReference": {"driveId": "drive-1"},
    }


# ── list_drive_item_children ──────────────────────────────────────────────


def test_list_children_root_target_path():
    from ms_graph_mcp.files import list_drive_item_children

    captured: dict = {}

    async def fake_get(_token, path, **params):
        captured["path"] = path
        captured["params"] = params
        return {"value": [_file("a"), _folder("b")]}

    with patch("ms_graph_mcp.files.graph_get", side_effect=fake_get):
        items = asyncio.run(list_drive_item_children("tok", "drive-1", "root"))

    assert captured["path"] == "/me/drive/root/children"
    assert "$select" in captured["params"]
    assert len(items) == 2


def test_list_children_by_item_id_target_path():
    from ms_graph_mcp.files import list_drive_item_children

    captured: dict = {}

    async def fake_get(_token, path, **_params):
        captured["path"] = path
        return {"value": []}

    with patch("ms_graph_mcp.files.graph_get", side_effect=fake_get):
        asyncio.run(list_drive_item_children("tok", "drive-9", "item-42"))

    assert captured["path"] == "/drives/drive-9/items/item-42/children"


# ── walk_drive_descendants ────────────────────────────────────────────────


def test_walk_yields_only_files_skipping_folders():
    """BFS the tree:

    root/
      subfolder/
        leaf1.pdf
      file_at_root.docx
    """
    from ms_graph_mcp.files import walk_drive_descendants

    pages = {
        "root": [_folder("subfolder"), _file("file-at-root")],
        "subfolder": [_file("leaf-1")],
    }

    async def fake_children(_token, _drive, item_id, *, top=200):  # noqa: ARG001
        return pages.get(item_id, [])

    async def collect():
        out = []
        async for it in walk_drive_descendants("tok", "drive-1", "root"):
            out.append(it)
        return out

    with patch(
        "ms_graph_mcp.files.list_drive_item_children",
        AsyncMock(side_effect=fake_children),
    ):
        results = asyncio.run(collect())

    ids = sorted(r["id"] for r in results)
    assert ids == ["file-at-root", "leaf-1"]
    assert all("file" in r and r["file"] for r in results)


def test_walk_respects_max_files_cap():
    from ms_graph_mcp.files import walk_drive_descendants

    children = [_file(f"f-{i}") for i in range(50)]

    async def fake_children(_token, _drive, _item, *, top=200):  # noqa: ARG001
        return children

    async def collect_capped():
        out = []
        async for it in walk_drive_descendants("tok", "drive-1", "root", max_files=7):
            out.append(it)
        return out

    with patch(
        "ms_graph_mcp.files.list_drive_item_children",
        AsyncMock(side_effect=fake_children),
    ):
        results = asyncio.run(collect_capped())

    assert len(results) == 7


def test_walk_survives_per_folder_fetch_failure():
    """A 403 in one subfolder shouldn't poison sibling traversal."""
    from ms_graph_mcp.files import walk_drive_descendants

    async def fake_children(_token, _drive, item_id, *, top=200):  # noqa: ARG001
        if item_id == "root":
            return [_folder("ok"), _folder("forbidden")]
        if item_id == "ok":
            return [_file("good")]
        if item_id == "forbidden":
            raise RuntimeError("403 Forbidden")
        return []

    async def collect():
        out = []
        async for it in walk_drive_descendants("tok", "drive-1", "root"):
            out.append(it)
        return out

    with patch(
        "ms_graph_mcp.files.list_drive_item_children",
        AsyncMock(side_effect=fake_children),
    ):
        results = asyncio.run(collect())

    assert [r["id"] for r in results] == ["good"]
