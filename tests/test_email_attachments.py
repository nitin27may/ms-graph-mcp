"""Track F (Wave 2b) — ``fetch_message_attachments`` non-tool helper.

Mocks the Graph boundary so no real network calls. Covers:
- isInline filter (inline images / signatures dropped)
- size cap (max_mb arg) — both server-reported and actual size
- MIME allow-list + extension fallback
- base64 decode of contentBytes
- referenceAttachment / itemAttachment skip (no contentBytes)
- list-call failure returns [] without raising
- per-attachment fetch failure doesn't poison the rest
"""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, patch

import pytest


def _meta(items: list[dict]) -> dict:
    return {"value": items}


def _att(
    *,
    aid: str = "AAA",
    name: str = "spec.pdf",
    mime: str = "application/pdf",
    size: int = 1024,
    inline: bool = False,
    odata_type: str = "#microsoft.graph.fileAttachment",
) -> dict:
    return {
        "id": aid,
        "name": name,
        "contentType": mime,
        "size": size,
        "isInline": inline,
        "@odata.type": odata_type,
    }


def _full(content: bytes) -> dict:
    return {"contentBytes": base64.b64encode(content).decode("ascii")}


def _patch_graph(list_resp, full_resps_by_id: dict[str, dict]):
    """Patch graph_get to return list_resp on the metadata call, then
    drain ``full_resps_by_id`` for the per-attachment downloads."""

    async def _graph_get(token, path, **kwargs):
        if path.endswith("/attachments"):
            return list_resp
        # Per-attachment fetch — extract the id from the path tail.
        aid = path.rsplit("/", 1)[-1]
        if aid in full_resps_by_id:
            resp = full_resps_by_id[aid]
            if isinstance(resp, Exception):
                raise resp
            return resp
        return {}

    return patch(
        "ms_graph_mcp.email.graph_get",
        side_effect=_graph_get,
    )


def _run(coro):
    return asyncio.run(coro)


# ── Filters ────────────────────────────────────────────────────────────────


def test_inline_attachments_filtered_out():
    """Signature-block logos and image-in-body decorations are
    isInline=true — they have zero retrieval value and would just
    bloat the spawned-source count. Filter must drop them at the
    list-response level."""
    from ms_graph_mcp.email import fetch_message_attachments

    list_resp = _meta(
        [
            _att(aid="A1", name="logo.png", mime="image/png", inline=True),
            _att(aid="A2", name="spec.pdf"),
        ]
    )
    full = {"A2": _full(b"%PDF-1.4 actual content")}
    with _patch_graph(list_resp, full):
        out = _run(fetch_message_attachments("tok", "msg-1"))
    assert [a["name"] for a in out] == ["spec.pdf"]


def test_reference_and_item_attachments_skipped():
    """Only ``fileAttachment`` carries contentBytes;
    referenceAttachment + itemAttachment both fail downstream extract,
    so we drop them before fetching."""
    from ms_graph_mcp.email import fetch_message_attachments

    list_resp = _meta(
        [
            _att(
                aid="R1",
                name="link.docx",
                mime="application/octet-stream",
                odata_type="#microsoft.graph.referenceAttachment",
            ),
            _att(
                aid="I1",
                name="forwarded.eml",
                mime="message/rfc822",
                odata_type="#microsoft.graph.itemAttachment",
            ),
            _att(aid="F1", name="spec.pdf"),
        ]
    )
    full = {"F1": _full(b"pdf bytes")}
    with _patch_graph(list_resp, full):
        out = _run(fetch_message_attachments("tok", "msg-1"))
    assert [a["id"] for a in out] == ["F1"]


def test_size_cap_drops_oversize_via_metadata():
    """If Graph reports size > max_mb in the listing, we never fetch
    the bytes — saves a download round-trip. Covers the common case
    of 50MB PowerPoint decks past our default 25MB cap."""
    from ms_graph_mcp.email import fetch_message_attachments

    over = 30 * 1024 * 1024
    list_resp = _meta(
        [
            _att(
                aid="BIG",
                name="big.pptx",
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                size=over,
            ),
            _att(
                aid="OK",
                name="brd.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                size=2048,
            ),
        ]
    )
    full = {"OK": _full(b"docx bytes")}
    with _patch_graph(list_resp, full):
        out = _run(fetch_message_attachments("tok", "msg-1", max_mb=25))
    assert [a["id"] for a in out] == ["OK"]


def test_size_cap_drops_oversize_after_decode():
    """A server lying about resourceSize can't blow up the embedding
    pipeline — we re-check post-decode and drop."""
    from ms_graph_mcp.email import fetch_message_attachments

    list_resp = _meta(
        [
            _att(aid="LIE", name="spec.pdf", size=1024),
        ]
    )
    big_payload = b"x" * (2 * 1024 * 1024)  # 2MB actual
    full = {"LIE": _full(big_payload)}
    with _patch_graph(list_resp, full):
        # 1MB cap forces post-decode drop
        out = _run(fetch_message_attachments("tok", "msg-1", max_mb=1))
    assert out == []


# ── MIME allow-list ────────────────────────────────────────────────────────


def test_unsupported_mime_dropped():
    """Videos / archives can't be embedded — drop without fetching."""
    from ms_graph_mcp.email import fetch_message_attachments

    list_resp = _meta(
        [
            _att(aid="V", name="walkthrough.mp4", mime="video/mp4"),
            _att(aid="Z", name="logs.zip", mime="application/zip"),
            _att(aid="P", name="spec.pdf"),
        ]
    )
    full = {"P": _full(b"pdf")}
    with _patch_graph(list_resp, full):
        out = _run(fetch_message_attachments("tok", "msg-1"))
    assert [a["id"] for a in out] == ["P"]


def test_extension_fallback_when_mime_is_octet_stream():
    """Some email clients send everything as application/octet-stream;
    fall back to the extension to recover the MIME type so .pdf / .docx
    attachments aren't dropped because of a sloppy sender."""
    from ms_graph_mcp.email import fetch_message_attachments

    list_resp = _meta(
        [
            _att(aid="A", name="brief.docx", mime="application/octet-stream"),
        ]
    )
    full = {"A": _full(b"docx bytes")}
    with _patch_graph(list_resp, full):
        out = _run(fetch_message_attachments("tok", "msg-1"))
    assert len(out) == 1
    # MIME normalised to the office form
    assert "wordprocessingml" in out[0]["contentType"]


# ── Decode ─────────────────────────────────────────────────────────────────


def test_content_bytes_decoded_to_raw_bytes():
    """Caller expects raw ``bytes`` to hash + extract — string b64 would
    break sha256()."""
    from ms_graph_mcp.email import fetch_message_attachments

    payload = b"hello-world-payload"
    list_resp = _meta([_att(aid="A", name="x.pdf")])
    full = {"A": _full(payload)}
    with _patch_graph(list_resp, full):
        out = _run(fetch_message_attachments("tok", "msg-1"))
    assert out[0]["content_bytes"] == payload
    assert out[0]["size"] == len(payload)


def test_corrupt_base64_drops_one_attachment_not_all():
    """Per-attachment failure must be isolated — a single bad blob
    can't kill the rest of the message's attachments."""
    from ms_graph_mcp.email import fetch_message_attachments

    list_resp = _meta(
        [
            _att(aid="BAD", name="b.pdf"),
            _att(aid="GOOD", name="g.pdf"),
        ]
    )
    full = {
        "BAD": {"contentBytes": "!!!not-base64!!!"},
        "GOOD": _full(b"good"),
    }
    with _patch_graph(list_resp, full):
        out = _run(fetch_message_attachments("tok", "msg-1"))
    assert [a["id"] for a in out] == ["GOOD"]


# ── Failure modes ──────────────────────────────────────────────────────────


def test_list_call_failure_returns_empty():
    """If Graph rejects the list call (auth issue, transient 5xx) the
    helper must return [] so the caller's spawn loop is a no-op — never
    raise into the gather pipeline and block the email itself."""
    from ms_graph_mcp.email import fetch_message_attachments

    with patch(
        "ms_graph_mcp.email.graph_get",
        AsyncMock(side_effect=RuntimeError("Graph 503")),
    ):
        out = _run(fetch_message_attachments("tok", "msg-1"))
    assert out == []


def test_per_attachment_fetch_failure_skips_only_that_one():
    """The list call succeeded, but one attachment fetch errored —
    skip that attachment, keep the rest."""
    from ms_graph_mcp.email import fetch_message_attachments

    list_resp = _meta(
        [
            _att(aid="ERR", name="e.pdf"),
            _att(aid="OK", name="o.pdf"),
        ]
    )
    full = {
        "ERR": RuntimeError("Graph 500"),
        "OK": _full(b"ok bytes"),
    }
    with _patch_graph(list_resp, full):
        out = _run(fetch_message_attachments("tok", "msg-1"))
    assert [a["id"] for a in out] == ["OK"]


def test_empty_message_returns_empty():
    from ms_graph_mcp.email import fetch_message_attachments

    with _patch_graph(_meta([]), {}):
        out = _run(fetch_message_attachments("tok", "msg-1"))
    assert out == []


# ── mail_list_attachments (agent surface) ────────────────────────────────────
# Distinct from fetch_message_attachments above: the agent-facing tool returns
# metadata only. Handing a model base64 file content is useless to it and
# enormously expensive in tokens — downloading stays in the internal tier.


class TestListEmailAttachmentsTool:
    async def test_returns_metadata_without_content(self):
        from ms_graph_mcp.email import ListEmailAttachmentsInput, mail_list_attachments

        payload = {
            "value": [
                {
                    "id": "a1",
                    "name": "spec.pdf",
                    "contentType": "application/pdf",
                    "size": 51200,
                    "isInline": False,
                }
            ]
        }
        with patch("ms_graph_mcp.email.graph_get", new=AsyncMock(return_value=payload)) as get:
            result = await mail_list_attachments(
                ListEmailAttachmentsInput(message_id="msg1"), {"access_token": "tok"}
            )

        assert result == [
            {
                "id": "a1",
                "name": "spec.pdf",
                "content_type": "application/pdf",
                "size_bytes": 51200,
            }
        ]
        # No contentBytes requested — that is the entire point of this tool.
        select = get.call_args.kwargs["$select"]
        assert "contentBytes" not in select
        assert get.call_args.args[1] == "/me/messages/msg1/attachments"

    async def test_inline_signature_images_are_excluded(self):
        from ms_graph_mcp.email import ListEmailAttachmentsInput, mail_list_attachments

        payload = {
            "value": [
                {"id": "a1", "name": "logo.png", "isInline": True, "size": 10},
                {"id": "a2", "name": "report.docx", "isInline": False, "size": 20},
            ]
        }
        with patch("ms_graph_mcp.email.graph_get", new=AsyncMock(return_value=payload)):
            result = await mail_list_attachments(
                ListEmailAttachmentsInput(message_id="msg1"), {"access_token": "tok"}
            )
        assert [a["name"] for a in result] == ["report.docx"]

    async def test_rejects_an_injected_message_id(self):
        from ms_graph_mcp.email import ListEmailAttachmentsInput, mail_list_attachments

        with patch("ms_graph_mcp.email.graph_get", new=AsyncMock()) as get:
            with pytest.raises(ValueError):
                await mail_list_attachments(
                    ListEmailAttachmentsInput(message_id="../../me/messages"),
                    {"access_token": "tok"},
                )
        get.assert_not_called()
