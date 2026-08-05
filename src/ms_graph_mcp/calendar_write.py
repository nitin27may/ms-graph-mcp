"""Calendar write and scheduling tools.

Two groups, and the split matters for the tier they land in:

**Writes** — create, update, cancel, respond. These mutate the user's calendar
and notify other people, so they sit in the write tier.

**Scheduling** — ``findMeetingTimes`` and ``getSchedule``. Both are POST because
they take a request body too large for a query string, but neither changes
anything. They are read-tier tools, and they are what turns a calendar the agent
can only look at into one it can actually reason about booking into.
"""

from __future__ import annotations

from enum import StrEnum

import httpx
from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_patch, graph_post, graph_post_no_content
from ms_graph_mcp.errors import graph_error_response, invalid_arguments
from ms_graph_mcp.odata import validate_graph_id
from ms_graph_mcp.tooling import (
    READ_ONLY,
    WRITE_CREATE,
    WRITE_DESTRUCTIVE,
    WRITE_UPDATE,
    tool,
)

# Graph accepts a naive local datetime plus a separate timeZone. Defaulting to
# UTC keeps the model from having to know the user's zone, and an explicit
# Windows time-zone name still works when it does.
_DEFAULT_TIMEZONE = "UTC"

# getSchedule encodes each interval as one digit. Handing "000220130" to a model
# is not useful; these are the meanings from the Graph docs.
_AVAILABILITY_CODES = {
    "0": "free",
    "1": "tentative",
    "2": "busy",
    "3": "out of office",
    "4": "unknown",
}


class AttendeeRole(StrEnum):
    """Why someone is on the invite."""

    required = "required"
    optional = "optional"
    resource = "resource"


class EventAttendee(BaseModel):
    """One invitee. ``resource`` is how a room or equipment is booked."""

    email: str = Field(description="Attendee's email address")
    name: str = Field(default="", description="Display name, if known")
    role: AttendeeRole = Field(
        default=AttendeeRole.required,
        description="required, optional, or resource for a room or equipment",
    )


class EventResponse(StrEnum):
    accept = "accept"
    decline = "decline"
    tentative = "tentativelyAccept"


def _attendee_payload(attendees: list[EventAttendee]) -> list[dict]:
    return [
        {
            "emailAddress": {"address": a.email, **({"name": a.name} if a.name else {})},
            "type": a.role.value,
        }
        for a in attendees
    ]


def _dtz(value: str, timezone: str) -> dict:
    return {"dateTime": value, "timeZone": timezone or _DEFAULT_TIMEZONE}


def _slim_created_event(e: dict) -> dict:
    return {
        "id": e.get("id", ""),
        "subject": e.get("subject", ""),
        "start": (e.get("start") or {}).get("dateTime", ""),
        "end": (e.get("end") or {}).get("dateTime", ""),
        "time_zone": (e.get("start") or {}).get("timeZone", ""),
        "web_link": e.get("webLink", ""),
        "join_url": (e.get("onlineMeeting") or {}).get("joinUrl", ""),
        "attendees": [
            (a.get("emailAddress") or {}).get("address", "") for a in (e.get("attendees") or [])
        ],
    }


# ── Write tier ────────────────────────────────────────────────────────────────


class CreateEventInput(BaseModel):
    subject: str = Field(description="Meeting title")
    start: str = Field(description="Start as ISO 8601 local time, e.g. 2026-08-12T14:00:00")
    end: str = Field(description="End as ISO 8601 local time, e.g. 2026-08-12T15:00:00")
    attendees: list[EventAttendee] = Field(
        default_factory=list, description="People and rooms to invite. Empty books time only."
    )
    body_html: str = Field(default="", description="Agenda or description as HTML")
    location: str = Field(default="", description="Location name, e.g. 'Room 4' or 'Zoom'")
    is_online_meeting: bool = Field(
        default=False, description="True adds a Microsoft Teams join link"
    )
    time_zone: str = Field(
        default=_DEFAULT_TIMEZONE,
        description="Windows time-zone name for start/end, e.g. 'GMT Standard Time'. Default UTC.",
    )


class UpdateEventInput(BaseModel):
    event_id: str = Field(description="The calendar event id to change")
    subject: str = Field(default="", description="New title. Omit to leave unchanged.")
    start: str = Field(default="", description="New start, ISO 8601. Requires end too.")
    end: str = Field(default="", description="New end, ISO 8601. Requires start too.")
    body_html: str = Field(default="", description="Replacement agenda as HTML")
    location: str = Field(default="", description="New location name")
    time_zone: str = Field(default=_DEFAULT_TIMEZONE, description="Time zone for start/end")


class CancelEventInput(BaseModel):
    event_id: str = Field(description="The calendar event id to cancel")
    comment: str = Field(
        default="", description="Note sent to attendees explaining the cancellation"
    )


class RespondToEventInput(BaseModel):
    event_id: str = Field(description="The calendar event id being responded to")
    response: EventResponse = Field(description="accept, decline, or tentativelyAccept")
    comment: str = Field(default="", description="Optional note sent with the reply")
    send_response: bool = Field(
        default=True, description="False replies without notifying the organiser"
    )


@tool(
    description=(
        "Create a calendar event for the signed-in user and invite people to it. Takes a subject, "
        "ISO 8601 start and end, and optional attendees, location, HTML agenda and a Teams link. "
        "Invitations are sent immediately, so confirm the time and attendee list with the user "
        "first. Use calendar_find_meeting_times beforehand to pick a slot everyone is free for. "
        "Requires Calendars.ReadWrite."
    ),
    annotations=WRITE_CREATE,
)
async def calendar_create_event(params: CreateEventInput, context: dict) -> dict:
    token = context["access_token"]
    body: dict = {
        "subject": params.subject,
        "start": _dtz(params.start, params.time_zone),
        "end": _dtz(params.end, params.time_zone),
    }
    if params.body_html:
        body["body"] = {"contentType": "HTML", "content": params.body_html}
    if params.location:
        body["location"] = {"displayName": params.location}
    if params.attendees:
        body["attendees"] = _attendee_payload(params.attendees)
    if params.is_online_meeting:
        body["isOnlineMeeting"] = True
        body["onlineMeetingProvider"] = "teamsForBusiness"
    try:
        created = await graph_post(token, "/me/events", body)
    except httpx.HTTPStatusError as exc:
        return graph_error_response(exc, scope="Calendars.ReadWrite", tool="calendar_create_event")
    return _slim_created_event(created)


@tool(
    description=(
        "Change an existing calendar event — its title, time, location or agenda. Only the fields "
        "supplied are altered; everything omitted stays as it was. Moving an event sends an update "
        "to every attendee, so confirm before calling. Start and end must be given together. "
        "Requires Calendars.ReadWrite."
    ),
    annotations=WRITE_UPDATE,
)
async def calendar_update_event(params: UpdateEventInput, context: dict) -> dict:
    token = context["access_token"]
    event_id = validate_graph_id(params.event_id, "event_id")
    if bool(params.start) != bool(params.end):
        return invalid_arguments(
            "start and end must be supplied together — Graph rejects a partial time change. "
            "Provide both, or neither to leave the time alone."
        )
    body: dict = {}
    if params.subject:
        body["subject"] = params.subject
    if params.start:
        body["start"] = _dtz(params.start, params.time_zone)
        body["end"] = _dtz(params.end, params.time_zone)
    if params.body_html:
        body["body"] = {"contentType": "HTML", "content": params.body_html}
    if params.location:
        body["location"] = {"displayName": params.location}
    if not body:
        return invalid_arguments("Nothing to update — supply at least one field to change.")
    try:
        updated = await graph_patch(token, f"/me/events/{event_id}", body)
    except httpx.HTTPStatusError as exc:
        return graph_error_response(exc, scope="Calendars.ReadWrite", tool="calendar_update_event")
    return _slim_created_event(updated)


@tool(
    description=(
        "Cancel a meeting the signed-in user organised, notifying every attendee with an optional "
        "note. The event is withdrawn from all attendees' calendars, so treat this as destructive "
        "and confirm with the user first. Only the organiser can cancel — attendees should use "
        "calendar_respond_to_event with decline instead. Requires Calendars.ReadWrite."
    ),
    annotations=WRITE_DESTRUCTIVE,
)
async def calendar_cancel_event(params: CancelEventInput, context: dict) -> dict:
    token = context["access_token"]
    event_id = validate_graph_id(params.event_id, "event_id")
    try:
        await graph_post_no_content(
            token,
            f"/me/events/{event_id}/cancel",
            {"Comment": params.comment} if params.comment else {},
        )
    except httpx.HTTPStatusError as exc:
        return graph_error_response(exc, scope="Calendars.ReadWrite", tool="calendar_cancel_event")
    return {"status": "cancelled", "event_id": event_id}


@tool(
    description=(
        "Reply to a meeting invitation on the signed-in user's behalf — accept, decline or "
        "tentativelyAccept — with an optional note to the organiser. Use for triaging invitations. "
        "This is the attendee's action; calendar_cancel_event is the organiser's and withdraws the "
        "meeting for everyone. Requires Calendars.ReadWrite."
    ),
    annotations=WRITE_UPDATE,
)
async def calendar_respond_to_event(params: RespondToEventInput, context: dict) -> dict:
    token = context["access_token"]
    event_id = validate_graph_id(params.event_id, "event_id")
    body: dict = {"sendResponse": params.send_response}
    if params.comment:
        body["comment"] = params.comment
    try:
        await graph_post_no_content(token, f"/me/events/{event_id}/{params.response.value}", body)
    except httpx.HTTPStatusError as exc:
        return graph_error_response(
            exc, scope="Calendars.ReadWrite", tool="calendar_respond_to_event"
        )
    return {"status": params.response.value, "event_id": event_id}


# ── Read tier — POST, but nothing is mutated ─────────────────────────────────


class FindMeetingTimesInput(BaseModel):
    attendees: list[EventAttendee] = Field(
        default_factory=list,
        description="Who must attend. Empty looks at the signed-in user's calendar only.",
    )
    duration_minutes: int = Field(default=30, description="Meeting length in minutes")
    window_start: str = Field(
        default="", description="Earliest acceptable start, ISO 8601. Defaults to now."
    )
    window_end: str = Field(default="", description="Latest acceptable end, ISO 8601")
    max_suggestions: int = Field(default=10, description="How many candidate slots to return")
    minimum_attendee_percentage: int = Field(
        default=50,
        description="Only suggest slots where at least this percentage of attendees are free (0-100)",
    )
    time_zone: str = Field(default=_DEFAULT_TIMEZONE, description="Time zone for the window")


class GetFreeBusyInput(BaseModel):
    emails: list[str] = Field(description="Email addresses to check availability for")
    start: str = Field(description="Start of the window, ISO 8601")
    end: str = Field(description="End of the window, ISO 8601")
    interval_minutes: int = Field(
        default=30, description="Granularity of each availability slot, 5 to 1440"
    )
    time_zone: str = Field(default=_DEFAULT_TIMEZONE, description="Time zone for start and end")


@tool(
    description=(
        "Ask Microsoft Graph to suggest meeting times when the given attendees are free, ranked by "
        "how likely everyone is to attend. Returns candidate slots with a confidence percentage "
        "and per-attendee availability. This reads calendars and books nothing — pass a chosen "
        "slot to calendar_create_event to actually schedule it. Requires Calendars.Read.Shared."
    ),
    annotations=READ_ONLY,
)
async def calendar_find_meeting_times(params: FindMeetingTimesInput, context: dict) -> dict:
    token = context["access_token"]
    if not 0 <= params.minimum_attendee_percentage <= 100:
        return invalid_arguments("minimum_attendee_percentage must be between 0 and 100.")
    body: dict = {
        "meetingDuration": f"PT{max(params.duration_minutes, 1)}M",
        "maxCandidates": params.max_suggestions,
        "minimumAttendeePercentage": float(params.minimum_attendee_percentage),
        "returnSuggestionReasons": True,
    }
    if params.attendees:
        body["attendees"] = _attendee_payload(params.attendees)
    if params.window_start and params.window_end:
        body["timeConstraint"] = {
            "activityDomain": "work",
            "timeSlots": [
                {
                    "start": _dtz(params.window_start, params.time_zone),
                    "end": _dtz(params.window_end, params.time_zone),
                }
            ],
        }
    try:
        data = await graph_post(token, "/me/findMeetingTimes", body)
    except httpx.HTTPStatusError as exc:
        return graph_error_response(
            exc, scope="Calendars.Read.Shared", tool="calendar_find_meeting_times"
        )

    suggestions = [
        {
            "start": ((s.get("meetingTimeSlot") or {}).get("start") or {}).get("dateTime", ""),
            "end": ((s.get("meetingTimeSlot") or {}).get("end") or {}).get("dateTime", ""),
            "time_zone": ((s.get("meetingTimeSlot") or {}).get("start") or {}).get("timeZone", ""),
            "confidence_percent": s.get("confidence", 0),
            "organizer_availability": s.get("organizerAvailability", ""),
            "reason": s.get("suggestionReason", ""),
            "unavailable_attendees": [
                ((a.get("attendee") or {}).get("emailAddress") or {}).get("address", "")
                for a in (s.get("attendeeAvailability") or [])
                if a.get("availability") not in ("free", "unknown")
            ],
        }
        for s in (data.get("meetingTimeSuggestions") or [])
    ]
    result: dict = {"suggestions": suggestions}
    # Graph explains an empty result rather than just returning nothing, and that
    # explanation is what tells the model whether to widen the window or drop an
    # attendee — so it must not be swallowed.
    if not suggestions:
        result["no_suggestions_reason"] = (
            data.get("emptySuggestionsReason")
            or "Graph returned no reason. Try a wider window or a lower "
            "minimum_attendee_percentage."
        )
    return result


@tool(
    description=(
        "Look up whether people are free or busy across a time window, without seeing what their "
        "meetings are. Returns per-person busy periods and a slot-by-slot free/tentative/busy/out "
        "of office breakdown. Use to answer 'when is X available'; calendar_find_meeting_times is "
        "better when the goal is a slot that suits a whole group. Requires Calendars.ReadBasic."
    ),
    annotations=READ_ONLY,
)
async def calendar_get_free_busy(params: GetFreeBusyInput, context: dict) -> dict:
    token = context["access_token"]
    if not params.emails:
        return invalid_arguments("Supply at least one email address to check.")
    if not 5 <= params.interval_minutes <= 1440:
        return invalid_arguments("interval_minutes must be between 5 and 1440.")
    body = {
        "schedules": params.emails,
        "startTime": _dtz(params.start, params.time_zone),
        "endTime": _dtz(params.end, params.time_zone),
        "availabilityViewInterval": params.interval_minutes,
    }
    try:
        data = await graph_post(token, "/me/calendar/getSchedule", body)
    except httpx.HTTPStatusError as exc:
        return graph_error_response(exc, scope="Calendars.ReadBasic", tool="calendar_get_free_busy")

    return {
        "interval_minutes": params.interval_minutes,
        "schedules": [
            {
                "email": s.get("scheduleId", ""),
                # availabilityView is a digit string, one per interval. Decoded
                # here because "000220130" tells a model nothing.
                "slots": [
                    _AVAILABILITY_CODES.get(ch, "unknown")
                    for ch in (s.get("availabilityView") or "")
                ],
                "busy_periods": [
                    {
                        "status": item.get("status", ""),
                        "start": (item.get("start") or {}).get("dateTime", ""),
                        "end": (item.get("end") or {}).get("dateTime", ""),
                        "subject": item.get("subject", ""),
                    }
                    for item in (s.get("scheduleItems") or [])
                    if item.get("status") != "free"
                ],
                "error": (s.get("error") or {}).get("message", ""),
            }
            for s in (data.get("value") or [])
        ],
    }


__all__ = [
    "calendar_cancel_event",
    "calendar_create_event",
    "calendar_find_meeting_times",
    "calendar_get_free_busy",
    "calendar_respond_to_event",
    "calendar_update_event",
]
