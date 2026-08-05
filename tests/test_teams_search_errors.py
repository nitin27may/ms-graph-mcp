"""chat_search_messages no longer hides failures as empty results.

The tool previously hand-rolled httpx and did `if resp.status_code != 200:
return []`. A permission problem, a throttle and a genuine no-match all looked
identical to the model — so an agent lacking Chat.Read would report "no messages
found" rather than "you do not have access", and the user would believe it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx

from ms_graph_mcp.teams import SearchTeamsMessagesInput, chat_search_messages

_CTX = {"access_token": "tok"}


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://graph.microsoft.com/v1.0/search/query")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


async def test_permission_failure_is_reported_not_disguised_as_no_results():
    with patch("ms_graph_mcp.teams.graph_post", new=AsyncMock()) as post:
        post.side_effect = _http_error(403)
        result = await chat_search_messages(SearchTeamsMessagesInput(query="claims"), _CTX)

    assert isinstance(result, dict), "a failure must not look like an empty result list"
    assert result["error"] == "SCOPE_DENIED"
    assert result["scope"] == "Chat.Read"
    assert result["retryable"] is False


async def test_throttling_is_reported_as_retryable():
    with patch("ms_graph_mcp.teams.graph_post", new=AsyncMock()) as post:
        post.side_effect = _http_error(429)
        result = await chat_search_messages(SearchTeamsMessagesInput(query="x"), _CTX)

    assert result["error"] == "THROTTLED"
    assert result["retryable"] is True


async def test_a_genuine_empty_result_is_still_an_empty_list():
    """The one case that legitimately means 'nothing matched'."""
    with patch("ms_graph_mcp.teams.graph_post", new=AsyncMock(return_value={"value": []})):
        result = await chat_search_messages(SearchTeamsMessagesInput(query="x"), _CTX)
    assert result == []


async def test_hits_are_flattened_out_of_the_search_envelope():
    payload = {
        "value": [
            {
                "hitsContainers": [
                    {
                        "hits": [
                            {
                                "resource": {
                                    "id": "m1",
                                    "body": {"content": "the claims deck is ready"},
                                    "from": {"user": {"displayName": "Priya"}},
                                    "createdDateTime": "2026-08-01T10:00:00Z",
                                    "webUrl": "https://teams/m1",
                                }
                            }
                        ]
                    }
                ]
            }
        ]
    }
    with patch("ms_graph_mcp.teams.graph_post", new=AsyncMock(return_value=payload)):
        result = await chat_search_messages(SearchTeamsMessagesInput(query="claims"), _CTX)

    assert len(result) == 1
    assert result[0]["from"] == "Priya"
    assert "claims deck" in result[0]["body"]


async def test_search_goes_through_the_shared_client():
    """Not raw httpx — that path lost the tracing span and the [Graph] logging."""
    with patch("ms_graph_mcp.teams.graph_post", new=AsyncMock(return_value={"value": []})) as post:
        await chat_search_messages(SearchTeamsMessagesInput(query="x", max_results=5), _CTX)

    token, path, body = post.call_args.args
    assert token == "tok"
    assert path == "/search/query"
    assert body["requests"][0]["entityTypes"] == ["chatMessage"]
    assert body["requests"][0]["size"] == 5
