"""Graph Teams tools — joined teams, channels, and message search."""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get, graph_post
from ms_graph_mcp.errors import graph_error_response
from ms_graph_mcp.tooling import READ_ONLY, tool


class SearchTeamsMessagesInput(BaseModel):
    query: str = Field(description="Search keywords to find in Teams messages")
    max_results: int = Field(10, description="Maximum messages to return")


class GetChannelMessagesInput(BaseModel):
    team_id: str = Field(description="The team ID")
    channel_id: str = Field(description="The channel ID")
    max_results: int = Field(20, description="Maximum messages to return")


class GetJoinedTeamsInput(BaseModel):
    max_results: int = Field(20, description="Maximum teams to return")


class GetTeamChannelsInput(BaseModel):
    team_id: str = Field(description="The team ID to list channels for")


@tool(
    description=(
        "Search Microsoft Teams messages the signed-in user can see, across every team and chat, "
        "by keyword. Returns message text, author, timestamp and a link to the message in Teams. "
        "Use for 'what did someone say about X'. chat_list_channel_messages is the tool for "
        "reading one channel in order instead of searching. Requires Chat.Read."
    ),
    annotations=READ_ONLY,
    aliases=("search_teams_messages",),
)
async def chat_search_messages(
    params: SearchTeamsMessagesInput, context: dict
) -> list[dict] | dict:
    token = context["access_token"]
    # Use Graph search API for cross-team message search
    search_body = {
        "requests": [
            {
                "entityTypes": ["chatMessage"],
                "query": {"queryString": params.query},
                "from": 0,
                "size": min(params.max_results, 25),
            }
        ]
    }
    try:
        data = await graph_post(token, "/search/query", search_body)
    except httpx.HTTPStatusError as exc:
        # Previously this swallowed every non-200 and returned [], so a
        # permission problem was indistinguishable from "no messages match".
        return graph_error_response(exc, scope="Chat.Read", tool="chat_search_messages")

    hits = []
    for result in data.get("value") or []:
        for hit in result.get("hitsContainers") or []:
            for h in hit.get("hits") or []:
                resource = h.get("resource", {})
                hits.append(
                    {
                        "id": resource.get("id", ""),
                        "body": (resource.get("body") or {}).get("content", "")[:500],
                        "from": (resource.get("from") or {}).get("user", {}).get("displayName", ""),
                        "created_at": resource.get("createdDateTime", ""),
                        "channel_display_name": resource.get("channelIdentity", {}).get(
                            "channelId", ""
                        ),
                        "web_url": resource.get("webUrl", ""),
                    }
                )
    return hits


@tool(
    description=(
        "Read the most recent messages in one Teams channel, newest first, given a team id and "
        "channel id from chat_list_teams and chat_list_channels. Returns text, author, timestamp "
        "and importance. Use to catch up on a channel; chat_search_messages is the tool for "
        "finding a message by keyword across everything. Requires ChannelMessage.Read.All."
    ),
    annotations=READ_ONLY,
    aliases=("get_channel_messages",),
)
async def chat_list_channel_messages(params: GetChannelMessagesInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        f"/teams/{params.team_id}/channels/{params.channel_id}/messages",
        **{
            "$top": min(params.max_results, 50),
            "$select": "id,body,from,createdDateTime,importance,webUrl",
        },
    )
    return [
        {
            "id": m.get("id", ""),
            "body": (m.get("body") or {}).get("content", "")[:500],
            "from": (m.get("from") or {}).get("user", {}).get("displayName", ""),
            "created_at": m.get("createdDateTime", ""),
            "importance": m.get("importance", "normal"),
            "web_url": m.get("webUrl", ""),
        }
        for m in (data.get("value") or [])
        if (m.get("body") or {}).get("contentType") != "html"
        or (m.get("body") or {}).get("content")
    ]


@tool(
    description=(
        "List the Microsoft Teams the signed-in user is a member of, with id, name and "
        "description. Start here for anything scoped to a team — the returned team id is what "
        "chat_list_channels takes, and a channel id from that is what reading messages needs. "
        "Requires Team.ReadBasic.All."
    ),
    annotations=READ_ONLY,
    aliases=("get_joined_teams",),
)
async def chat_list_teams(params: GetJoinedTeamsInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me/joinedTeams",
        **{"$select": "id,displayName,description", "$top": params.max_results},
    )
    return [
        {
            "id": t.get("id", ""),
            "name": t.get("displayName", ""),
            "description": t.get("description", ""),
        }
        for t in (data.get("value") or [])
    ]


@tool(
    description=(
        "List the channels in one Microsoft Teams team, with id, name and description. Takes a "
        "team id from chat_list_teams. The returned channel id is what "
        "chat_list_channel_messages needs to read a conversation. Requires "
        "Channel.ReadBasic.All."
    ),
    annotations=READ_ONLY,
    aliases=("get_team_channels",),
)
async def chat_list_channels(params: GetTeamChannelsInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        f"/teams/{params.team_id}/channels",
        **{"$select": "id,displayName,description"},
    )
    return [
        {
            "id": c.get("id", ""),
            "name": c.get("displayName", ""),
            "description": c.get("description", ""),
        }
        for c in (data.get("value") or [])
    ]
