"""OneDrive write helpers — sanitization + simple PUT + upload session +
overwrite-in-place + 412 eTag handling.

httpx is mocked at the AsyncClient boundary so no real Graph calls.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ── sanitize_filename ──────────────────────────────────────────────────────


def test_sanitize_strips_illegal_chars():
    from ms_graph_mcp.files_write import sanitize_filename

    assert sanitize_filename('foo/bar?<>"baz.pdf') == "foo-bar-baz.pdf"


def test_sanitize_collapses_runs_and_trims_dots():
    from ms_graph_mcp.files_write import sanitize_filename

    assert sanitize_filename(".....foo***.bar.....") == "foo-.bar"


def test_sanitize_returns_default_for_empty_input():
    from ms_graph_mcp.files_write import sanitize_filename

    assert sanitize_filename("") == "document"
    assert sanitize_filename("///") == "document"


def test_sanitize_caps_long_names_preserving_extension():
    from ms_graph_mcp.files_write import sanitize_filename

    long_name = ("a" * 250) + ".docx"
    out = sanitize_filename(long_name)
    assert out.endswith(".docx")
    assert len(out) <= 200


# ── upload_file_to_drive (simple PUT) ─────────────────────────────────────


def _mock_put_response(status: int, body: dict | None = None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.is_success = 200 <= status < 300
    resp.text = "" if resp.is_success else "graph error body"
    resp.json.return_value = body or {
        "id": "item-1",
        "name": "doc.pdf",
        "webUrl": "https://onedrive/x",
        "size": 1024,
        "eTag": '"abc"',
        "lastModifiedDateTime": "2026-04-30T10:00:00Z",
        "parentReference": {"driveId": "drive-1"},
    }
    return resp


def _patch_async_client(resp):
    """Build a patch target replacing httpx.AsyncClient context with a mock
    whose .put returns the supplied response."""
    client = MagicMock()
    client.put = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=client), client


def _put_result(status: int):
    """A GraphResult as graph_put_raw would return it."""
    from ms_graph_mcp.client import GraphResult

    payload = (
        {
            "id": "item-1",
            "name": "report.pdf",
            "webUrl": "https://contoso-my.sharepoint.com/report.pdf",
            "size": 5,
            "eTag": '"abc"',
            "lastModifiedDateTime": "2026-04-30T10:00:00Z",
            "parentReference": {"driveId": "drive-1"},
        }
        if 200 <= status < 300
        else None
    )
    return GraphResult(status_code=status, ok=200 <= status < 300, text="", _payload=payload)


def test_upload_simple_put_success():
    """Simple upload now goes through client.py:graph_put_raw, not raw httpx."""
    from ms_graph_mcp.files_write import upload_file_to_drive

    with patch("ms_graph_mcp.files_write.graph_put_raw", new=AsyncMock()) as put:
        put.return_value = _put_result(201)
        result = asyncio.run(
            upload_file_to_drive(
                "tok",
                "WorkGraph/Documents",
                "report.pdf",
                b"hello",
                "application/pdf",
            )
        )

    token, path, content, mime = put.await_args.args
    # The path must encode each segment, end with :/content, and carry the
    # rename conflict-behavior so an existing filename gets a "-1" suffix
    # rather than a 409.
    assert token == "tok"
    assert "/me/drive/root:/WorkGraph/Documents/report.pdf:/content" in path
    assert "@microsoft.graph.conflictBehavior=rename" in path
    assert content == b"hello"
    assert mime == "application/pdf"

    # Returns the slimmed driveItem with the eTag preserved.
    assert result["id"] == "item-1"
    assert result["drive_id"] == "drive-1"
    assert result["etag"] == '"abc"'


def test_upload_simple_put_failure_raises_typed_error():
    from ms_graph_mcp.files_write import OneDriveError, upload_file_to_drive

    with patch("ms_graph_mcp.files_write.graph_put_raw", new=AsyncMock()) as put:
        put.return_value = _put_result(403)
        with pytest.raises(OneDriveError) as exc_info:
            asyncio.run(
                upload_file_to_drive(
                    "tok",
                    "WorkGraph/Documents",
                    "report.pdf",
                    b"hello",
                    "application/pdf",
                )
            )

    assert exc_info.value.status_code == 403
    assert "403" in str(exc_info.value)


def test_upload_large_file_uses_upload_session():
    """>4 MiB → createUploadSession + chunked PUTs to the returned URL."""
    from ms_graph_mcp.files_write import upload_file_to_drive

    big = b"x" * (5 * 1024 * 1024 + 1024)  # 5 MiB + 1 KiB

    async def fake_post(_token, path, _body):  # noqa: ARG001
        assert "createUploadSession" in path
        return {"uploadUrl": "https://upload.microsoft.com/abc"}

    chunk_resp = _mock_put_response(
        202,
        body={
            "id": "item-big",
            "name": "big.pdf",
            "size": len(big),
            "parentReference": {"driveId": "drive-1"},
            "webUrl": "https://onedrive/big",
            "eTag": '"big-etag"',
        },
    )
    factory, client = _patch_async_client(chunk_resp)

    with (
        patch("ms_graph_mcp.files_write.graph_post", side_effect=fake_post),
        patch("ms_graph_mcp.files_write.httpx.AsyncClient", factory),
    ):
        result = asyncio.run(
            upload_file_to_drive(
                "tok",
                "WorkGraph/Documents",
                "big.pdf",
                big,
                "application/pdf",
            )
        )

    # Each chunk lands on the upload session URL with a Content-Range
    # header. Total chunks = ceil(size / 5 MiB) = 2 for a 5 MiB+1 KiB body.
    calls = client.put.await_args_list
    assert len(calls) >= 2
    for call in calls:
        assert call.args[0] == "https://upload.microsoft.com/abc"
        assert "Content-Range" in call.kwargs["headers"]
    assert result["id"] == "item-big"
    assert result["etag"] == '"big-etag"'


# ── update_drive_item_content ─────────────────────────────────────────────


def test_update_in_place_includes_if_match():
    from ms_graph_mcp.files_write import update_drive_item_content

    put_mock = AsyncMock(return_value=_put_result(200))

    with patch("ms_graph_mcp.files_write.graph_put_raw", put_mock):
        asyncio.run(
            update_drive_item_content(
                "tok",
                "drive-1",
                "item-1",
                b"updated",
                "text/markdown",
                etag='"old"',
            )
        )

    url = put_mock.await_args.args[1]
    headers = put_mock.await_args.kwargs["extra_headers"] or {}
    assert "/drives/drive-1/items/item-1/content" in url
    assert headers["If-Match"] == '"old"'


def test_update_412_raises_conflict_error():
    from ms_graph_mcp.files_write import (
        OneDriveConflictError,
        update_drive_item_content,
    )

    put_mock = AsyncMock(return_value=_put_result(412))

    with patch("ms_graph_mcp.files_write.graph_put_raw", put_mock):
        with pytest.raises(OneDriveConflictError):
            asyncio.run(
                update_drive_item_content(
                    "tok",
                    "drive-1",
                    "item-1",
                    b"x",
                    "text/markdown",
                    etag='"stale"',
                )
            )


def test_update_other_4xx_raises_generic_error():
    from ms_graph_mcp.files_write import (
        OneDriveError,
        update_drive_item_content,
    )

    put_mock = AsyncMock(return_value=_put_result(403))

    with patch("ms_graph_mcp.files_write.graph_put_raw", put_mock):
        with pytest.raises(OneDriveError) as exc_info:
            asyncio.run(
                update_drive_item_content("tok", "drive-1", "item-1", b"x", "text/markdown")
            )
    assert exc_info.value.status_code == 403


def test_update_requires_drive_and_item_ids():
    from ms_graph_mcp.files_write import (
        OneDriveError,
        update_drive_item_content,
    )

    with pytest.raises(OneDriveError):
        asyncio.run(update_drive_item_content("tok", "", "item-1", b"x", "text/markdown"))
    with pytest.raises(OneDriveError):
        asyncio.run(update_drive_item_content("tok", "drive-1", "", b"x", "text/markdown"))


# ── ensure_folder_exists ──────────────────────────────────────────────────


def test_ensure_folder_recurses_through_segments():
    from ms_graph_mcp.files_write import ensure_folder_exists

    posted: list[tuple[str, dict]] = []

    async def fake_post(_token, path, body):
        posted.append((path, body))
        return {
            "id": f"id-{body['name']}",
            "name": body["name"],
            "parentReference": {"driveId": "drive-1"},
        }

    with patch("ms_graph_mcp.files_write.graph_post", side_effect=fake_post):
        result = asyncio.run(ensure_folder_exists("tok", "WorkGraph/Documents"))

    # Two POSTs — root/children for "WorkGraph", then items/id-WorkGraph/children for "Documents".
    assert len(posted) == 2
    assert posted[0][0] == "/me/drive/root/children"
    assert posted[0][1]["name"] == "WorkGraph"
    assert posted[1][0] == "/me/drive/items/id-WorkGraph/children"
    assert posted[1][1]["name"] == "Documents"
    assert result["id"] == "id-Documents"


def test_ensure_folder_409_falls_back_to_lookup():
    """Pre-existing folder → 409 → GET by path → continue."""
    from ms_graph_mcp.files_write import ensure_folder_exists

    response_409 = MagicMock(spec=httpx.Response)
    response_409.status_code = 409
    error_409 = httpx.HTTPStatusError("conflict", request=MagicMock(), response=response_409)

    async def fake_post(_token, _path, _body):
        raise error_409

    async def fake_get(_token, path, **_params):
        # Path lookup of the existing folder.
        return {
            "id": "existing-id",
            "name": "WorkGraph",
            "parentReference": {"driveId": "drive-1"},
        }

    with (
        patch("ms_graph_mcp.files_write.graph_post", side_effect=fake_post),
        patch("ms_graph_mcp.files_write.graph_get", side_effect=fake_get),
    ):
        result = asyncio.run(ensure_folder_exists("tok", "WorkGraph"))

    assert result["id"] == "existing-id"


# ── drive_id parameterization — SharePoint / team drives ──────────────────


def test_upload_with_drive_id_uses_drives_base_url():
    """drive_id present → URL uses /drives/{id}/… instead of /me/drive/…"""
    from ms_graph_mcp.client import GraphResult
    from ms_graph_mcp.files_write import upload_file_to_drive

    put_mock = AsyncMock(
        return_value=GraphResult(
            status_code=201,
            ok=True,
            text="",
            _payload={
                "id": "sp-item-1",
                "name": "doc.md",
                "webUrl": "https://contoso.sharepoint.com/doc.md",
                "size": 50,
                "eTag": '"sp-etag"',
                "parentReference": {"driveId": "sp-drive-abc"},
            },
        )
    )

    with patch("ms_graph_mcp.files_write.graph_put_raw", put_mock):
        result = asyncio.run(
            upload_file_to_drive(
                "tok",
                "Shared Documents/Plans",
                "doc.md",
                b"# Plan",
                "text/markdown",
                drive_id="sp-drive-abc",
            )
        )

    import urllib.parse

    url = urllib.parse.unquote(put_mock.await_args.args[1])
    assert "/drives/sp-drive-abc/root:/Shared Documents/Plans/doc.md:/content" in url
    assert "/me/drive" not in url
    assert result["drive_id"] == "sp-drive-abc"


def test_upload_without_drive_id_uses_me_drive():
    """drive_id absent → URL keeps the original /me/drive/… base."""
    from ms_graph_mcp.files_write import upload_file_to_drive

    put_mock = AsyncMock(return_value=_put_result(201))

    with patch("ms_graph_mcp.files_write.graph_put_raw", put_mock):
        asyncio.run(
            upload_file_to_drive(
                "tok",
                "WorkGraph/Docs",
                "doc.pdf",
                b"hello",
                "application/pdf",
            )
        )

    url = put_mock.await_args.args[1]
    assert "/me/drive/root:/WorkGraph/Docs/doc.pdf:/content" in url
    assert "/drives/" not in url


def test_ensure_folder_with_drive_id_posts_to_drives_base():
    """drive_id → folder creation uses /drives/{id}/root/children."""
    from ms_graph_mcp.files_write import ensure_folder_exists

    posted: list[tuple[str, dict]] = []

    async def fake_post(_token, path, body):
        posted.append((path, body))
        return {
            "id": f"id-{body['name']}",
            "name": body["name"],
            "parentReference": {"driveId": "sp-drive-abc"},
        }

    with patch("ms_graph_mcp.files_write.graph_post", side_effect=fake_post):
        result = asyncio.run(
            ensure_folder_exists("tok", "Shared Documents/Plans", drive_id="sp-drive-abc")
        )

    assert posted[0][0] == "/drives/sp-drive-abc/root/children"
    assert "/me/drive" not in posted[0][0]
    assert result["id"] == "id-Plans"


def test_ensure_folder_with_drive_id_409_uses_drives_get():
    """drive_id + 409 → GET path resolves via /drives/{id}/root:/…"""
    from ms_graph_mcp.files_write import ensure_folder_exists

    response_409 = MagicMock(spec=httpx.Response)
    response_409.status_code = 409
    error_409 = httpx.HTTPStatusError("conflict", request=MagicMock(), response=response_409)

    get_paths: list[str] = []

    async def fake_post(_token, _path, _body):
        raise error_409

    async def fake_get(_token, path, **_params):
        get_paths.append(path)
        return {
            "id": "sp-existing-id",
            "name": "Shared Documents",
            "parentReference": {"driveId": "sp-drive-abc"},
        }

    with (
        patch("ms_graph_mcp.files_write.graph_post", side_effect=fake_post),
        patch("ms_graph_mcp.files_write.graph_get", side_effect=fake_get),
    ):
        result = asyncio.run(
            ensure_folder_exists("tok", "Shared Documents", drive_id="sp-drive-abc")
        )

    assert result["id"] == "sp-existing-id"
    assert any("/drives/sp-drive-abc/root:/" in p for p in get_paths)
    assert not any("/me/drive" in p for p in get_paths)
