"""Contract tests for the Graph client helpers added so callers stop hand-rolling
their own httpx (probe status, full-URL/nextLink GET, raw OneNote page create)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from ms_graph_mcp.client import (
    graph_get_url,
    graph_post_no_content,
    graph_probe_status,
    graph_try_get,
)
from ms_graph_mcp.onenote import create_onenote_page

_GET = "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=abc"


def _mock_client(*, get=None, post=None):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if get is not None:
        client.get = AsyncMock(return_value=get)
    if post is not None:
        client.post = AsyncMock(return_value=post)
    return client


def _resp(*, status: int = 200, payload: dict | None = None, headers: dict | None = None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.raise_for_status = MagicMock()
    r.json.return_value = payload if payload is not None else {}
    return r


# ── graph_probe_status ─────────────────────────────────────────────────────────


async def test_graph_probe_status_returns_status_code():
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        with patch("httpx.AsyncClient", return_value=_mock_client(get=_resp(status=404))) as cls:
            status = await graph_probe_status("tok", "/users/u@x/messages/m1?$select=id")
    assert status == 404
    # Probe GET does NOT raise on non-200 (status is classified by the caller).
    assert "/users/u@x/messages/m1" in cls.return_value.get.call_args.args[0]


async def test_graph_probe_status_none_on_transport_error():
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        client = _mock_client()
        client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        with patch("httpx.AsyncClient", return_value=client):
            assert await graph_probe_status("tok", "/users/u@x/events/e1") is None


# ── graph_get_url ──────────────────────────────────────────────────────────────


async def test_graph_get_url_returns_json():
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        resp = _resp(payload={"value": [1, 2], "@odata.nextLink": None})
        with patch("httpx.AsyncClient", return_value=_mock_client(get=resp)) as cls:
            out = await graph_get_url("tok", _GET)
    assert out == {"value": [1, 2], "@odata.nextLink": None}
    assert cls.return_value.get.call_args.args[0] == _GET


async def test_graph_get_url_retries_once_on_429(monkeypatch):
    # First call → 429 with Retry-After; second → 200. asyncio.sleep is stubbed.
    slept: list = []

    async def _no_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr("ms_graph_mcp.client.asyncio.sleep", _no_sleep)

    throttled = _resp(status=429, headers={"Retry-After": "2"})
    ok = _resp(payload={"value": ["page2"]})
    client = _mock_client()
    client.get = AsyncMock(side_effect=[throttled, ok])
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        with patch("httpx.AsyncClient", return_value=client):
            out = await graph_get_url("tok", _GET)
    assert out == {"value": ["page2"]}
    assert client.get.await_count == 2
    assert slept == [2.0]  # honoured Retry-After


# ── graph_get_url host allowlist (Phase 0 item 7, agentic audit) ────────────────
#
# graph_get_url takes a full caller-supplied URL (unlike every other helper in
# this file, which is pinned to _GRAPH_BASE by construction) and attaches the
# caller's Graph bearer token to it. Without a host check, a value that ends
# up here from a compromised/misused caller could redirect a delegated
# token to an arbitrary host — SSRF + token exfiltration.


async def test_graph_get_url_rejects_non_graph_host():
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        with patch("httpx.AsyncClient") as cls:
            try:
                await graph_get_url("tok", "https://evil.example.com/steal?token=x")
                raised = False
            except ValueError:
                raised = True
    assert raised
    # The bearer must never even reach an HTTP client for a rejected host.
    cls.assert_not_called()


async def test_graph_get_url_rejects_graph_lookalike_host():
    """A URL that merely CONTAINS the Graph host as a substring (path,
    query, or subdomain trick) must not pass a naive substring check."""
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        with patch("httpx.AsyncClient") as cls:
            for bad_url in (
                "https://evil.example.com/?u=https://graph.microsoft.com/v1.0",
                "https://graph.microsoft.com.evil.example.com/v1.0/me",
                "http://graph.microsoft.com/v1.0/me",  # http, not https
            ):
                try:
                    await graph_get_url("tok", bad_url)
                    raised = False
                except ValueError:
                    raised = True
                assert raised, f"expected rejection for {bad_url!r}"
    cls.assert_not_called()


async def test_graph_get_url_accepts_real_graph_url():
    """The happy path — a genuine nextLink under _GRAPH_BASE — must still work
    (regression guard against the allowlist being too strict)."""
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        resp = _resp(payload={"value": []})
        with patch("httpx.AsyncClient", return_value=_mock_client(get=resp)):
            out = await graph_get_url("tok", _GET)
    assert out == {"value": []}


# ── create_onenote_page ─────────────────────────────────────────────────────────


async def test_create_onenote_page_posts_html_and_returns_raw():
    raw = {"id": "p1", "title": "T", "links": {"oneNoteWebUrl": {"href": "https://x"}}}
    with patch("ms_graph_mcp.config.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        with patch("httpx.AsyncClient", return_value=_mock_client(post=_resp(payload=raw))) as cls:
            out = await create_onenote_page(
                "tok", section_id="sec-9", page_title="Title", content_html="<p>body</p>"
            )
    assert out == raw  # raw Graph page object returned unchanged
    call = cls.return_value.post.call_args
    assert "/me/onenote/sections/sec-9/pages" in call.args[0]
    assert call.kwargs["headers"]["Content-Type"] == "text/html"
    body = call.kwargs["content"].decode("utf-8")
    assert "<title>Title</title>" in body and "<p>body</p>" in body


# ── graph_post_no_content ──────────────────────────────────────────────────────
# Graph's action endpoints (sendMail, reply, forward, accept, cancel, …) answer
# 202 with an empty body, which graph_post cannot handle because it ends in
# resp.json(). This helper is what stops callers reaching for raw httpx and
# losing the tracing span and [Graph] error logging along the way.


async def test_graph_post_no_content_succeeds_on_empty_202():
    resp = _resp(status=202)
    resp.is_success = True
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        client = _mock_client(post=resp)
        with patch("httpx.AsyncClient", return_value=client):
            result = await graph_post_no_content("tok", "/me/sendMail", {"message": {}})

    assert result is None
    # The body must never be read — that is the whole point of the helper.
    resp.json.assert_not_called()
    called_url = client.post.call_args.args[0]
    assert called_url == "https://graph.microsoft.com/v1.0/me/sendMail"


async def test_graph_post_no_content_sends_auth_and_extra_headers():
    resp = _resp(status=202)
    resp.is_success = True
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        client = _mock_client(post=resp)
        with patch("httpx.AsyncClient", return_value=client):
            await graph_post_no_content(
                "tok", "/me/events/e1/cancel", {"comment": "x"}, {"If-Match": 'W/"1"'}
            )

    headers = client.post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok"
    assert headers["If-Match"] == 'W/"1"'


async def test_graph_post_no_content_raises_on_error_status():
    resp = _resp(status=403)
    resp.is_success = False
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "forbidden", request=MagicMock(), response=MagicMock()
    )
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        with patch("httpx.AsyncClient", return_value=_mock_client(post=resp)):
            with patch("ms_graph_mcp.client._log_error") as log_error:
                try:
                    await graph_post_no_content("tok", "/me/sendMail", {})
                except httpx.HTTPStatusError:
                    pass
                else:  # pragma: no cover
                    raise AssertionError("expected HTTPStatusError")

    # A failed action must still be logged with the Graph error body.
    log_error.assert_called_once()


async def test_graph_post_no_content_defaults_body_to_empty_object():
    """Several action endpoints take no body at all; Graph wants {} not null."""
    resp = _resp(status=202)
    resp.is_success = True
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        client = _mock_client(post=resp)
        with patch("httpx.AsyncClient", return_value=client):
            await graph_post_no_content("tok", "/me/events/e1/accept")

    assert client.post.call_args.kwargs["json"] == {}


# ── graph_try_get ─────────────────────────────────────────────────────────────
# graph_get raises on any non-2xx, which is right when a failure is an error and
# wrong wherever the status itself is a signal. A 403 from the JoinWebUrl filter
# means "you attended this meeting but did not organise it" and selects a
# different lookup strategy — meetings.py hand-rolled 14 httpx clients to be able
# to see that, losing the tracing span and [Graph] logging in the process.


async def test_graph_try_get_returns_parsed_json_on_success():
    resp = _resp(status=200, payload={"value": [{"id": "1"}]})
    resp.is_success = True
    resp.text = ""
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        with patch("httpx.AsyncClient", return_value=_mock_client(get=resp)):
            result = await graph_try_get("tok", "/me/onlineMeetings", **{"$top": 5})

    assert result.ok is True
    assert result.status_code == 200
    assert result.json() == {"value": [{"id": "1"}]}


async def test_graph_try_get_does_not_raise_on_403():
    """The whole reason this helper exists."""
    resp = _resp(status=403)
    resp.is_success = False
    resp.text = "forbidden"
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        with patch("httpx.AsyncClient", return_value=_mock_client(get=resp)):
            result = await graph_try_get("tok", "/me/onlineMeetings")

    assert result.status_code == 403
    assert result.ok is False
    resp.raise_for_status.assert_not_called()


async def test_graph_try_get_json_is_empty_dict_on_failure():
    """Callers branch on .ok then read .json() — it must not explode."""
    resp = _resp(status=404)
    resp.is_success = False
    resp.text = ""
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        with patch("httpx.AsyncClient", return_value=_mock_client(get=resp)):
            result = await graph_try_get("tok", "/me/x")

    assert result.json() == {}


async def test_graph_try_get_returns_text_for_non_json_accept():
    """Transcript content is VTT, not JSON."""
    resp = _resp(status=200)
    resp.is_success = True
    resp.text = "WEBVTT\n\n00:00.000 --> 00:02.000\nHello"
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        client = _mock_client(get=resp)
        with patch("httpx.AsyncClient", return_value=client):
            result = await graph_try_get(
                "tok", "/me/onlineMeetings/m1/transcripts/t1/content", accept="*/*"
            )

    assert "WEBVTT" in result.text
    # Accept must reach Graph, or it wraps the VTT in a JSON envelope.
    assert client.get.call_args.kwargs["headers"]["Accept"] == "*/*"
    # And no JSON parse should have been attempted on a non-JSON body.
    resp.json.assert_not_called()


async def test_graph_try_get_survives_a_success_with_an_unparseable_body():
    resp = _resp(status=200)
    resp.is_success = True
    resp.text = "not json"
    resp.json.side_effect = ValueError("no")
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        with patch("httpx.AsyncClient", return_value=_mock_client(get=resp)):
            result = await graph_try_get("tok", "/me/x")

    assert result.ok is True
    assert result.json() == {}


async def test_graph_try_get_encodes_odata_params_literally():
    """The $ must survive — the whole OData surface depends on it."""
    resp = _resp(status=200, payload={})
    resp.is_success = True
    resp.text = ""
    with patch("ms_graph_mcp.client.get_config") as cfg:
        cfg.return_value.disable_ssl_verify = False
        client = _mock_client(get=resp)
        with patch("httpx.AsyncClient", return_value=client):
            await graph_try_get("tok", "/me/onlineMeetings", **{"$filter": "JoinWebUrl eq 'x'"})

    url = client.get.call_args.args[0]
    assert "$filter=" in url, "OData $ was percent-encoded"
