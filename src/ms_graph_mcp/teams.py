"""Graph Teams tools — joined teams, channels, and message search."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get
from ms_graph_mcp.config import get_config
from ms_graph_mcp.tooling import tool


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
    description="Search across all Teams channels the user has access to. Returns message text, author, and channel."
)
async def search_teams_messages(params: SearchTeamsMessagesInput, context: dict) -> list[dict]:
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
    import httpx

    async with httpx.AsyncClient(verify=not get_config().disable_ssl_verify, timeout=30) as client:
        resp = await client.post(
            "https://graph.microsoft.com/v1.0/search/query",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=search_body,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()

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


@tool(description="Get recent messages from a specific Teams channel.")
async def get_channel_messages(params: GetChannelMessagesInput, context: dict) -> list[dict]:
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


@tool(description="Get the list of Teams the user has joined.")
async def get_joined_teams(params: GetJoinedTeamsInput, context: dict) -> list[dict]:
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


@tool(description="Get the channels available in a specific Teams team.")
async def get_team_channels(params: GetTeamChannelsInput, context: dict) -> list[dict]:
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
