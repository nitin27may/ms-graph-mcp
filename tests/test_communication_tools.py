"""Mail actions, Teams chats, unified search, OneNote pages and contacts.

Grouped because they landed together. The cases that matter here are the ones a
mocked Graph can catch but a reviewer easily misses: which tools are subject to
the recipient allowlist, which Graph request shapes are rejected outright, and
the three lookalike people-search tools staying distinguishable.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ms_graph_mcp.chats import (
    ChatMessagesInput,
    ListChatsInput,
    SendChatMessageInput,
    chat_list,
    chat_list_messages,
    chat_send_message,
)
from ms_graph_mcp.config import get_config
from ms_graph_mcp.contacts import (
    CreateContactInput,
    SearchContactsInput,
    people_create_contact,
    people_search_contacts,
)
from ms_graph_mcp.context import current_request_context
from ms_graph_mcp.email import (
    ForwardEmailInput,
    MarkEmailReadInput,
    ReplyEmailInput,
    mail_forward,
    mail_mark_read,
    mail_reply,
    mail_reply_all,
)
from ms_graph_mcp.onenote import GetOnenotePageInput, notes_get_page_content
from ms_graph_mcp.search import SearchEntity, SearchQueryInput, search_query

_CTX = {"access_token": "tok"}


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://graph.microsoft.com/v1.0/x")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status, request=request)
    )


# ── mail actions ──────────────────────────────────────────────────────────────


async def test_reply_and_reply_all_hit_different_endpoints():
    for fn, expected in ((mail_reply, "reply"), (mail_reply_all, "replyAll")):
        with patch("ms_graph_mcp.email.graph_post_no_content", new=AsyncMock()) as post:
            await fn(ReplyEmailInput(message_id="m1", comment_html="<p>ok</p>"), _CTX)
        assert post.call_args.args[1] == f"/me/messages/m1/{expected}"
        assert post.call_args.args[2] == {"comment": "<p>ok</p>"}


async def test_forward_is_subject_to_the_recipient_allowlist():
    """Forwarding lets the caller choose recipients, so it is an exfiltration path.

    mail_send is already gated. Forwarding an existing message to an arbitrary
    address is the same risk and must go through the same gate.
    """
    with (
        patch.object(get_config(), "send_email_allowed_domains", "contoso.com"),
        patch("ms_graph_mcp.email.graph_post_no_content", new=AsyncMock()) as post,
    ):
        result = await mail_forward(
            ForwardEmailInput(message_id="m1", to_recipients=["attacker@evil.com"]), _CTX
        )
    post.assert_not_called()
    assert result["error"] == "recipient_not_allowed"
    assert result["retryable"] is False


async def test_reply_is_not_gated_by_the_allowlist():
    """The thread fixes who receives a reply — the caller cannot redirect it."""
    with (
        patch.object(get_config(), "send_email_allowed_domains", "contoso.com"),
        patch("ms_graph_mcp.email.graph_post_no_content", new=AsyncMock()) as post,
    ):
        result = await mail_reply(ReplyEmailInput(message_id="m1", comment_html="hi"), _CTX)
    post.assert_called_once()
    assert result["status"] == "sent"


async def test_forward_builds_the_recipient_shape_graph_expects():
    with (
        patch.object(get_config(), "send_email_allowed_domains", ""),
        patch("ms_graph_mcp.email.graph_post_no_content", new=AsyncMock()) as post,
    ):
        await mail_forward(
            ForwardEmailInput(
                message_id="m1", to_recipients=["a@x.com", "b@x.com"], comment_html="FYI"
            ),
            _CTX,
        )
    body = post.call_args.args[2]
    assert body["toRecipients"] == [
        {"emailAddress": {"address": "a@x.com"}},
        {"emailAddress": {"address": "b@x.com"}},
    ]
    assert body["comment"] == "FYI"


async def test_forward_requires_a_recipient():
    with patch("ms_graph_mcp.email.graph_post_no_content", new=AsyncMock()) as post:
        result = await mail_forward(ForwardEmailInput(message_id="m1", to_recipients=[]), _CTX)
    post.assert_not_called()
    assert result["error"] == "INVALID_ARGUMENTS"


async def test_mark_read_toggles_both_ways():
    for is_read in (True, False):
        with patch("ms_graph_mcp.email.graph_patch", new=AsyncMock()) as p:
            result = await mail_mark_read(
                MarkEmailReadInput(message_id="m1", is_read=is_read), _CTX
            )
        assert p.call_args.args[2] == {"isRead": is_read}
        assert result["status"] == ("read" if is_read else "unread")


# ── Teams chats ───────────────────────────────────────────────────────────────


async def test_chat_list_labels_a_one_to_one_by_its_members():
    """A 1:1 has no topic; without member names the model sees only opaque ids."""
    payload = {
        "value": [
            {"id": "c1", "chatType": "oneOnOne", "members": [{"displayName": "Priya Sharma"}]},
            {"id": "c2", "chatType": "group", "topic": "Claims triage", "members": []},
        ]
    }
    with patch("ms_graph_mcp.chats.graph_get", new=AsyncMock(return_value=payload)):
        result = await chat_list(ListChatsInput(), _CTX)
    assert result[0]["name"] == "Priya Sharma"
    assert result[1]["name"] == "Claims triage"


async def test_chat_list_falls_back_when_there_is_nothing_to_label_with():
    payload = {"value": [{"id": "c1", "chatType": "group"}]}
    with patch("ms_graph_mcp.chats.graph_get", new=AsyncMock(return_value=payload)):
        result = await chat_list(ListChatsInput(), _CTX)
    assert result[0]["name"] == "(untitled chat)"


async def test_chat_messages_drops_bodyless_system_events():
    """Joins and renames carry no content and are pure noise to a model."""
    payload = {
        "value": [
            {"id": "m1", "body": {"content": "real message"}},
            {"id": "m2", "body": {"content": ""}},
            {"id": "m3"},
        ]
    }
    with patch("ms_graph_mcp.chats.graph_get", new=AsyncMock(return_value=payload)):
        result = await chat_list_messages(ChatMessagesInput(chat_id="c1"), _CTX)
    assert [m["id"] for m in result] == ["m1"]


async def test_chat_send_rejects_an_empty_message():
    with patch("ms_graph_mcp.chats.graph_post", new=AsyncMock()) as post:
        result = await chat_send_message(
            SendChatMessageInput(chat_id="c1", message_html="   "), _CTX
        )
    post.assert_not_called()
    assert result["error"] == "INVALID_ARGUMENTS"


async def test_chat_send_reports_the_right_scope_on_denial():
    with patch("ms_graph_mcp.chats.graph_post", new=AsyncMock()) as post:
        post.side_effect = _http_error(403)
        result = await chat_send_message(
            SendChatMessageInput(chat_id="c1", message_html="hi"), _CTX
        )
    assert result["scope"] == "ChatMessage.Send"


# ── unified search ────────────────────────────────────────────────────────────


async def test_search_refuses_a_combination_graph_rejects():
    """Graph will not search mail and SharePoint together, and says so unhelpfully.

    Catching it locally turns an opaque 400 into an instruction the model can
    act on.
    """
    with patch("ms_graph_mcp.search.graph_post", new=AsyncMock()) as post:
        result = await search_query(
            SearchQueryInput(
                query="budget",
                entity_types=[SearchEntity.message, SearchEntity.drive_item],
            ),
            _CTX,
        )
    post.assert_not_called()
    assert result["error"] == "INVALID_ARGUMENTS"
    assert "two calls" in result["message"]


async def test_search_allows_types_within_one_group():
    with patch("ms_graph_mcp.search.graph_post", new=AsyncMock(return_value={"value": []})) as post:
        await search_query(
            SearchQueryInput(query="x", entity_types=[SearchEntity.message, SearchEntity.event]),
            _CTX,
        )
    assert post.call_args.args[2]["requests"][0]["entityTypes"] == ["message", "event"]


async def test_search_flattens_hits_across_containers():
    payload = {
        "value": [
            {
                "hitsContainers": [
                    {
                        "total": 42,
                        "hits": [
                            {
                                "rank": 1,
                                "summary": "the claims deck",
                                "resource": {
                                    "@odata.type": "#microsoft.graph.driveItem",
                                    "id": "d1",
                                    "name": "claims.pptx",
                                    "webUrl": "https://x/claims.pptx",
                                },
                            }
                        ],
                    }
                ]
            }
        ]
    }
    with patch("ms_graph_mcp.search.graph_post", new=AsyncMock(return_value=payload)):
        result = await search_query(SearchQueryInput(query="claims"), _CTX)
    hit = result["hits"][0]
    assert hit["type"] == "driveItem"
    assert hit["title"] == "claims.pptx"
    assert hit["url"] == "https://x/claims.pptx"
    assert result["total_matches"] == 42


async def test_search_rejects_an_empty_entity_list():
    with patch("ms_graph_mcp.search.graph_post", new=AsyncMock()) as post:
        result = await search_query(SearchQueryInput(query="x", entity_types=[]), _CTX)
    post.assert_not_called()
    assert result["error"] == "INVALID_ARGUMENTS"


# ── OneNote page content ──────────────────────────────────────────────────────


async def test_page_content_uses_the_text_helper_and_flags_truncation():
    """Page bodies are HTML, not JSON — graph_get_text, same as transcript VTT."""
    with patch(
        "ms_graph_mcp.onenote.graph_get_text", new=AsyncMock(return_value="<p>x</p>" * 20000)
    ) as get:
        result = await notes_get_page_content(GetOnenotePageInput(page_id="p1"), _CTX)
    assert get.call_args.args[1] == "/me/onenote/pages/p1/content"
    assert result["truncated"] is True
    assert len(result["content_html"]) == 50_000


# ── contacts ──────────────────────────────────────────────────────────────────


async def test_contact_search_escapes_an_odata_quote():
    """O'Brien must not break the filter expression."""
    with patch("ms_graph_mcp.contacts.graph_get", new=AsyncMock(return_value={"value": []})) as get:
        await people_search_contacts(SearchContactsInput(query="O'Brien"), _CTX)
    assert "O''Brien" in get.call_args.kwargs["$filter"]


async def test_create_contact_wraps_the_email_as_graph_expects():
    with patch("ms_graph_mcp.contacts.graph_post", new=AsyncMock(return_value={})) as post:
        await people_create_contact(
            CreateContactInput(given_name="Jane", surname="Doe", email="jane@ext.com"), _CTX
        )
    body = post.call_args.args[2]
    assert body["emailAddresses"] == [{"address": "jane@ext.com", "name": "Jane Doe"}]


async def test_create_contact_requires_a_first_name():
    with patch("ms_graph_mcp.contacts.graph_post", new=AsyncMock()) as post:
        result = await people_create_contact(CreateContactInput(given_name="  "), _CTX)
    post.assert_not_called()
    assert result["error"] == "INVALID_ARGUMENTS"


def test_the_three_lookalike_people_tools_explain_how_they_differ():
    """A model cannot pick between them unless each says what the others are.

    people_search reads /me/people, directory_search_users reads /users, and
    people_list_contacts reads /me/contacts. Only the last holds people with no
    tenant account at all.
    """
    from ms_graph_mcp.tooling import get_registry

    registry = get_registry()
    descriptions = {
        n: registry.get(n).description
        for n in ("people_search", "directory_search_users", "people_list_contacts")
    }
    # Each must name at least one sibling so the model can tell them apart.
    assert "directory_search_users" in descriptions["people_search"]
    assert "people_search" in descriptions["directory_search_users"]
    for sibling in ("people_search", "directory_search_users"):
        assert sibling in descriptions["people_list_contacts"]


# ── tier placement ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "mail_reply",
        "mail_reply_all",
        "mail_forward",
        "mail_mark_read",
        "chat_send_message",
        "people_create_contact",
    ],
)
async def test_new_write_tools_refused_without_scope(name, call_tool):
    cv = current_request_context.set({"access_token": "tok", "write_scope": False})
    try:
        result = await call_tool(name, {})
    finally:
        current_request_context.reset(cv)
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == "write_scope_required"


def test_sends_are_not_marked_idempotent():
    """A retried send posts twice — clients must not treat these as safe to repeat."""
    from ms_graph_mcp.tooling import get_registry

    registry = get_registry()
    for name in ("mail_reply", "mail_reply_all", "mail_forward", "chat_send_message"):
        assert registry.get(name).annotations.idempotent is False
