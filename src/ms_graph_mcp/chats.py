"""Microsoft Teams chat tools — 1:1 and group chats.

Distinct from ``teams.py``, which covers channels. Channels are the public,
team-scoped conversations; chats are the direct and small-group ones, and in
practice most Teams conversation happens here.

**Polling note.** Microsoft imposes a once-per-day polling limit on Teams
resources in the API terms of use, and treats a violation as a breach. The tool
descriptions deliberately do not invite an agent to poll for new messages;
anything needing that must use change notifications instead.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get, graph_post
from ms_graph_mcp.errors import graph_error_response, invalid_arguments
from ms_graph_mcp.odata import validate_graph_id
from ms_graph_mcp.tooling import READ_ONLY, WRITE_SEND, tool

_CHAT_SELECT = "id,topic,chatType,createdDateTime,lastUpdatedDateTime,webUrl"


class ListChatsInput(BaseModel):
    max_results: int = Field(20, description="Maximum chats to return (1-50)")


class ChatMessagesInput(BaseModel):
    chat_id: str = Field(description="The chat id, from chat_list")
    max_results: int = Field(20, description="Maximum messages to return (1-50)")


class SendChatMessageInput(BaseModel):
    chat_id: str = Field(description="The chat id to post into, from chat_list")
    message_html: str = Field(description="Message content as HTML")


class ChatMembersInput(BaseModel):
    chat_id: str = Field(description="The chat id, from chat_list")


def _chat_label(c: dict) -> str:
    """A usable name for a chat.

    Only group chats carry a topic, and even then it is often unset. For a 1:1
    the useful label is the other person, so fall back to member names — without
    it the model sees a list of opaque ids and cannot tell the user which chat
    is which.
    """
    if c.get("topic"):
        return c["topic"]
    names = [
        (m.get("displayName") or "").strip()
        for m in (c.get("members") or [])
        if (m.get("displayName") or "").strip()
    ]
    return ", ".join(names) if names else "(untitled chat)"


def _slim_message(m: dict) -> dict:
    return {
        "id": m.get("id", ""),
        "from": ((m.get("from") or {}).get("user") or {}).get("displayName", ""),
        "body": (m.get("body") or {}).get("content", "")[:2000],
        "created_at": m.get("createdDateTime", ""),
        "web_url": m.get("webUrl", ""),
    }


@tool(
    description=(
        "List the signed-in user's Microsoft Teams chats — the 1:1 and group conversations, not "
        "team channels. Returns id, a display label, chat type and when it was last active, most "
        "recent first. Use chat_list_channel_messages for channel conversations instead. The "
        "returned chat id is what the other chat tools take. Requires Chat.Read."
    ),
    annotations=READ_ONLY,
)
async def chat_list(params: ListChatsInput, context: dict) -> list[dict] | dict:
    token = context["access_token"]
    try:
        data = await graph_get(
            token,
            "/me/chats",
            **{
                "$top": min(max(params.max_results, 1), 50),
                "$expand": "members",
                "$orderby": "lastMessagePreview/createdDateTime desc",
            },
        )
    except httpx.HTTPStatusError as exc:
        return graph_error_response(exc, scope="Chat.Read", tool="chat_list")
    return [
        {
            "id": c.get("id", ""),
            "name": _chat_label(c),
            "type": c.get("chatType", ""),
            "last_active": c.get("lastUpdatedDateTime", ""),
            "web_url": c.get("webUrl", ""),
        }
        for c in (data.get("value") or [])
    ]


@tool(
    description=(
        "Read recent messages from one Teams chat, newest first, given a chat id from chat_list. "
        "Returns sender, text and timestamp per message. Use to catch up on a conversation before "
        "replying. chat_search_messages is the tool for finding a message by keyword across every "
        "chat and channel. Do not call repeatedly to watch for new messages. Requires Chat.Read."
    ),
    annotations=READ_ONLY,
)
async def chat_list_messages(params: ChatMessagesInput, context: dict) -> list[dict] | dict:
    token = context["access_token"]
    chat_id = validate_graph_id(params.chat_id, "chat_id")
    try:
        data = await graph_get(
            token,
            f"/chats/{chat_id}/messages",
            **{"$top": min(max(params.max_results, 1), 50)},
        )
    except httpx.HTTPStatusError as exc:
        return graph_error_response(exc, scope="Chat.Read", tool="chat_list_messages")
    # System messages (joins, renames) carry no body and are noise to a model.
    return [
        _slim_message(m) for m in (data.get("value") or []) if (m.get("body") or {}).get("content")
    ]


@tool(
    description=(
        "Send a message to a Teams chat as the signed-in user, given a chat id from chat_list. "
        "Content is HTML. The message is posted immediately and visible to everyone in the chat, "
        "so confirm both the wording and which chat with the user first. This posts to a 1:1 or "
        "group chat, not to a team channel. Requires ChatMessage.Send."
    ),
    annotations=WRITE_SEND,
)
async def chat_send_message(params: SendChatMessageInput, context: dict) -> dict:
    token = context["access_token"]
    chat_id = validate_graph_id(params.chat_id, "chat_id")
    if not params.message_html.strip():
        return invalid_arguments("Message content cannot be empty.")
    try:
        sent = await graph_post(
            token,
            f"/chats/{chat_id}/messages",
            {"body": {"contentType": "html", "content": params.message_html}},
        )
    except httpx.HTTPStatusError as exc:
        return graph_error_response(exc, scope="ChatMessage.Send", tool="chat_send_message")
    return {
        "status": "sent",
        "chat_id": chat_id,
        "message_id": sent.get("id", ""),
        "web_url": sent.get("webUrl", ""),
    }


@tool(
    description=(
        "List who is in a Teams chat, with display name and email, given a chat id from chat_list. "
        "Use to check who will see a message before sending it, or to identify the other party in "
        "a 1:1 whose label is unclear. Requires Chat.Read."
    ),
    annotations=READ_ONLY,
)
async def chat_list_members(params: ChatMembersInput, context: dict) -> list[dict] | dict:
    token = context["access_token"]
    chat_id = validate_graph_id(params.chat_id, "chat_id")
    try:
        data = await graph_get(token, f"/chats/{chat_id}/members")
    except httpx.HTTPStatusError as exc:
        return graph_error_response(exc, scope="Chat.Read", tool="chat_list_members")
    return [
        {
            "id": m.get("userId", ""),
            "name": m.get("displayName", ""),
            "email": m.get("email", ""),
        }
        for m in (data.get("value") or [])
    ]


__all__ = ["chat_list", "chat_list_members", "chat_list_messages", "chat_send_message"]
