"""Calendar write and scheduling tools.

Two things get particular attention here:

  * The **tier split**. `find_meeting_times` and `get_free_busy` are POST but
    mutate nothing, so they belong in the read tier. Getting that wrong would
    either hide them behind a write scope they do not need, or — worse in the
    other direction — put a mutation on the always-on surface.
  * **Payload shape.** Graph's calendar bodies are deeply nested
    (`start.dateTime` + `start.timeZone`, `attendees[].emailAddress.address`),
    and a mistake there fails only against the live API.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ms_graph_mcp.allowlists import READ_TOOL_NAMES, WRITE_TOOL_NAMES
from ms_graph_mcp.calendar_write import (
    AttendeeRole,
    CancelEventInput,
    CreateEventInput,
    EventAttendee,
    EventResponse,
    FindMeetingTimesInput,
    GetFreeBusyInput,
    RespondToEventInput,
    UpdateEventInput,
    calendar_cancel_event,
    calendar_create_event,
    calendar_find_meeting_times,
    calendar_get_free_busy,
    calendar_respond_to_event,
    calendar_update_event,
)
from ms_graph_mcp.context import current_request_context

_CTX = {"access_token": "tok"}


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://graph.microsoft.com/v1.0/me/events")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status, request=request)
    )


# ── tier placement ────────────────────────────────────────────────────────────


def test_scheduling_tools_are_read_tier_despite_being_post():
    """They take a body too big for a query string. They still change nothing."""
    assert "calendar_find_meeting_times" in READ_TOOL_NAMES
    assert "calendar_get_free_busy" in READ_TOOL_NAMES
    assert "calendar_find_meeting_times" not in WRITE_TOOL_NAMES
    assert "calendar_get_free_busy" not in WRITE_TOOL_NAMES


def test_mutating_tools_are_write_tier():
    for name in (
        "calendar_create_event",
        "calendar_update_event",
        "calendar_cancel_event",
        "calendar_respond_to_event",
    ):
        assert name in WRITE_TOOL_NAMES
        assert name not in READ_TOOL_NAMES


def test_only_cancel_is_marked_destructive():
    """Cancelling withdraws a meeting from other people's calendars."""
    from ms_graph_mcp.tooling import get_registry

    registry = get_registry()
    assert registry.get("calendar_cancel_event").annotations.destructive is True
    for name in ("calendar_create_event", "calendar_update_event", "calendar_respond_to_event"):
        assert registry.get(name).annotations.destructive is False


# ── create ────────────────────────────────────────────────────────────────────


async def test_create_event_builds_the_nested_graph_body():
    with patch("ms_graph_mcp.calendar_write.graph_post", new=AsyncMock()) as post:
        post.return_value = {"id": "e1", "subject": "Sync", "start": {"dateTime": "x"}}
        await calendar_create_event(
            CreateEventInput(
                subject="Sync",
                start="2026-08-12T14:00:00",
                end="2026-08-12T15:00:00",
                attendees=[
                    EventAttendee(email="a@contoso.com", name="Alice"),
                    EventAttendee(email="room4@contoso.com", role=AttendeeRole.resource),
                ],
                location="Room 4",
                is_online_meeting=True,
            ),
            _CTX,
        )

    body = post.call_args.args[2]
    assert body["start"] == {"dateTime": "2026-08-12T14:00:00", "timeZone": "UTC"}
    assert body["location"] == {"displayName": "Room 4"}
    assert body["attendees"][0]["emailAddress"] == {"address": "a@contoso.com", "name": "Alice"}
    assert body["attendees"][0]["type"] == "required"
    # A room is booked by giving it type=resource, not by naming it in location.
    assert body["attendees"][1]["type"] == "resource"
    assert body["isOnlineMeeting"] is True
    assert body["onlineMeetingProvider"] == "teamsForBusiness"


async def test_create_event_omits_a_name_that_was_not_supplied():
    """Graph rejects a null name; the key must be absent, not empty."""
    with patch("ms_graph_mcp.calendar_write.graph_post", new=AsyncMock()) as post:
        post.return_value = {}
        await calendar_create_event(
            CreateEventInput(
                subject="s",
                start="2026-08-12T14:00:00",
                end="2026-08-12T15:00:00",
                attendees=[EventAttendee(email="a@contoso.com")],
            ),
            _CTX,
        )
    assert post.call_args.args[2]["attendees"][0]["emailAddress"] == {"address": "a@contoso.com"}


async def test_create_event_honours_an_explicit_timezone():
    with patch("ms_graph_mcp.calendar_write.graph_post", new=AsyncMock()) as post:
        post.return_value = {}
        await calendar_create_event(
            CreateEventInput(
                subject="s",
                start="2026-08-12T14:00:00",
                end="2026-08-12T15:00:00",
                time_zone="GMT Standard Time",
            ),
            _CTX,
        )
    assert post.call_args.args[2]["start"]["timeZone"] == "GMT Standard Time"


async def test_create_event_reports_a_denial_with_the_scope():
    with patch("ms_graph_mcp.calendar_write.graph_post", new=AsyncMock()) as post:
        post.side_effect = _http_error(403)
        result = await calendar_create_event(
            CreateEventInput(subject="s", start="a", end="b"), _CTX
        )
    assert result["error"] == "SCOPE_DENIED"
    assert result["scope"] == "Calendars.ReadWrite"
    assert result["retryable"] is False


# ── update ────────────────────────────────────────────────────────────────────


async def test_update_event_sends_only_the_supplied_fields():
    with patch("ms_graph_mcp.calendar_write.graph_patch", new=AsyncMock()) as patch_call:
        patch_call.return_value = {"id": "e1"}
        await calendar_update_event(UpdateEventInput(event_id="e1", subject="New"), _CTX)
    body = patch_call.call_args.args[2]
    assert body == {"subject": "New"}, "an omitted field must not be sent as empty"


async def test_update_event_rejects_a_half_specified_time():
    """Graph accepts start without end and produces a nonsense event."""
    for kwargs in ({"start": "2026-08-12T14:00:00"}, {"end": "2026-08-12T15:00:00"}):
        with patch("ms_graph_mcp.calendar_write.graph_patch", new=AsyncMock()) as patch_call:
            result = await calendar_update_event(UpdateEventInput(event_id="e1", **kwargs), _CTX)
        patch_call.assert_not_called()
        assert result["error"] == "INVALID_ARGUMENTS"
        assert result["retryable"] is True


async def test_update_event_rejects_an_empty_change():
    with patch("ms_graph_mcp.calendar_write.graph_patch", new=AsyncMock()) as patch_call:
        result = await calendar_update_event(UpdateEventInput(event_id="e1"), _CTX)
    patch_call.assert_not_called()
    assert result["error"] == "INVALID_ARGUMENTS"


async def test_update_event_rejects_an_injected_id():
    with pytest.raises(ValueError):
        await calendar_update_event(UpdateEventInput(event_id="../../me/events", subject="x"), _CTX)


# ── cancel and respond ────────────────────────────────────────────────────────


async def test_cancel_event_posts_a_comment():
    with patch("ms_graph_mcp.calendar_write.graph_post_no_content", new=AsyncMock()) as post:
        result = await calendar_cancel_event(
            CancelEventInput(event_id="e1", comment="Rescheduling"), _CTX
        )
    path, body = post.call_args.args[1], post.call_args.args[2]
    assert path == "/me/events/e1/cancel"
    assert body == {"Comment": "Rescheduling"}
    assert result["status"] == "cancelled"


@pytest.mark.parametrize(
    "response,expected_path",
    [
        (EventResponse.accept, "/me/events/e1/accept"),
        (EventResponse.decline, "/me/events/e1/decline"),
        (EventResponse.tentative, "/me/events/e1/tentativelyAccept"),
    ],
)
async def test_respond_hits_the_right_action_endpoint(response, expected_path):
    with patch("ms_graph_mcp.calendar_write.graph_post_no_content", new=AsyncMock()) as post:
        await calendar_respond_to_event(RespondToEventInput(event_id="e1", response=response), _CTX)
    assert post.call_args.args[1] == expected_path
    assert post.call_args.args[2]["sendResponse"] is True


# ── findMeetingTimes ──────────────────────────────────────────────────────────


async def test_find_meeting_times_converts_minutes_to_iso_duration():
    """Graph wants ISO 8601; the model should not have to know that."""
    with patch("ms_graph_mcp.calendar_write.graph_post", new=AsyncMock()) as post:
        post.return_value = {"meetingTimeSuggestions": []}
        await calendar_find_meeting_times(FindMeetingTimesInput(duration_minutes=90), _CTX)
    assert post.call_args.args[2]["meetingDuration"] == "PT90M"


async def test_find_meeting_times_flattens_suggestions():
    payload = {
        "meetingTimeSuggestions": [
            {
                "confidence": 100,
                "organizerAvailability": "free",
                "suggestionReason": "Everyone is free.",
                "meetingTimeSlot": {
                    "start": {"dateTime": "2026-08-12T14:00:00", "timeZone": "UTC"},
                    "end": {"dateTime": "2026-08-12T15:00:00", "timeZone": "UTC"},
                },
                "attendeeAvailability": [
                    {"availability": "free", "attendee": {"emailAddress": {"address": "a@x.com"}}},
                    {"availability": "busy", "attendee": {"emailAddress": {"address": "b@x.com"}}},
                ],
            }
        ]
    }
    with patch("ms_graph_mcp.calendar_write.graph_post", new=AsyncMock(return_value=payload)):
        result = await calendar_find_meeting_times(FindMeetingTimesInput(), _CTX)

    suggestion = result["suggestions"][0]
    assert suggestion["start"] == "2026-08-12T14:00:00"
    assert suggestion["confidence_percent"] == 100
    # Only the people who cannot make it are worth the tokens.
    assert suggestion["unavailable_attendees"] == ["b@x.com"]


async def test_find_meeting_times_surfaces_graphs_reason_for_no_suggestions():
    """The reason is what tells the model to widen the window rather than give up."""
    payload = {"meetingTimeSuggestions": [], "emptySuggestionsReason": "AttendeesUnavailable"}
    with patch("ms_graph_mcp.calendar_write.graph_post", new=AsyncMock(return_value=payload)):
        result = await calendar_find_meeting_times(FindMeetingTimesInput(), _CTX)
    assert result["suggestions"] == []
    assert result["no_suggestions_reason"] == "AttendeesUnavailable"


async def test_find_meeting_times_explains_an_empty_result_graph_did_not_explain():
    with patch(
        "ms_graph_mcp.calendar_write.graph_post",
        new=AsyncMock(return_value={"meetingTimeSuggestions": []}),
    ):
        result = await calendar_find_meeting_times(FindMeetingTimesInput(), _CTX)
    assert "wider window" in result["no_suggestions_reason"]


async def test_find_meeting_times_validates_the_percentage():
    with patch("ms_graph_mcp.calendar_write.graph_post", new=AsyncMock()) as post:
        result = await calendar_find_meeting_times(
            FindMeetingTimesInput(minimum_attendee_percentage=150), _CTX
        )
    post.assert_not_called()
    assert result["error"] == "INVALID_ARGUMENTS"


# ── getSchedule ───────────────────────────────────────────────────────────────


async def test_free_busy_decodes_the_availability_digit_string():
    """ "000220130" is meaningless to a model; the words are not."""
    payload = {
        "value": [
            {
                "scheduleId": "a@contoso.com",
                "availabilityView": "01234",
                "scheduleItems": [
                    {
                        "status": "busy",
                        "subject": "Standup",
                        "start": {"dateTime": "2026-08-12T09:00:00"},
                        "end": {"dateTime": "2026-08-12T09:30:00"},
                    },
                    {"status": "free", "start": {}, "end": {}},
                ],
            }
        ]
    }
    with patch("ms_graph_mcp.calendar_write.graph_post", new=AsyncMock(return_value=payload)):
        result = await calendar_get_free_busy(
            GetFreeBusyInput(emails=["a@contoso.com"], start="s", end="e"), _CTX
        )

    schedule = result["schedules"][0]
    assert schedule["slots"] == ["free", "tentative", "busy", "out of office", "unknown"]
    # Free periods are not busy periods, and listing them would be noise.
    assert len(schedule["busy_periods"]) == 1
    assert schedule["busy_periods"][0]["subject"] == "Standup"


async def test_free_busy_rejects_an_out_of_range_interval():
    """Graph's own limits are 5 to 1440; catching it here avoids a round trip."""
    for interval in (1, 2000):
        with patch("ms_graph_mcp.calendar_write.graph_post", new=AsyncMock()) as post:
            result = await calendar_get_free_busy(
                GetFreeBusyInput(emails=["a@x.com"], start="s", end="e", interval_minutes=interval),
                _CTX,
            )
        post.assert_not_called()
        assert result["error"] == "INVALID_ARGUMENTS"


async def test_free_busy_rejects_an_empty_email_list():
    with patch("ms_graph_mcp.calendar_write.graph_post", new=AsyncMock()) as post:
        result = await calendar_get_free_busy(GetFreeBusyInput(emails=[], start="s", end="e"), _CTX)
    post.assert_not_called()
    assert result["error"] == "INVALID_ARGUMENTS"


# ── schema ────────────────────────────────────────────────────────────────────


def test_nested_attendee_schema_resolves():
    """The first tools with a nested model and an enum — what the $defs fix was for."""
    import re

    from ms_graph_mcp.tooling import get_registry

    schema = get_registry().get("calendar_create_event").parameters
    refs = set(re.findall(r'"\$ref":\s*"#/\$defs/([^"]+)"', json.dumps(schema)))
    assert refs, "expected the nested attendee model to produce a $ref"
    assert not refs - set(schema.get("$defs", {})), "schema references a stripped definition"
    assert schema["$defs"]["AttendeeRole"]["enum"] == ["required", "optional", "resource"]


@pytest.mark.parametrize(
    "name",
    [
        "calendar_create_event",
        "calendar_update_event",
        "calendar_cancel_event",
        "calendar_respond_to_event",
    ],
)
async def test_write_tools_refused_without_scope(name, call_tool):
    cv = current_request_context.set({"access_token": "tok", "write_scope": False})
    try:
        result = await call_tool(name, {})
    finally:
        current_request_context.reset(cv)
    assert result.is_error is True
    assert json.loads(result.content[0].text)["error"] == "write_scope_required"
