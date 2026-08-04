"""Graph Calendar tools — calendar events, meeting details, attendees."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get
from ms_graph_mcp.tooling import tool

_SELECT_EVENT = "id,subject,start,end,location,organizer,attendees,bodyPreview,isOnlineMeeting,onlineMeeting,webLink"
_SELECT_BRIEF = "id,subject,start,end,organizer,isOnlineMeeting"


class GetUpcomingMeetingsInput(BaseModel):
    days_ahead: int = Field(1, description="Number of days ahead to look (1-14)")
    max_results: int = Field(10, description="Maximum meetings to return")


class GetMeetingDetailsInput(BaseModel):
    meeting_id: str = Field(description="The calendar event ID")


class GetMeetingAttendeesInput(BaseModel):
    meeting_id: str = Field(description="The calendar event ID")


class GetCalendarEventsRangeInput(BaseModel):
    start_date: str = Field(description="Start date ISO 8601 (e.g. 2024-01-15)")
    end_date: str = Field(description="End date ISO 8601 (e.g. 2024-01-22)")
    max_results: int = Field(20, description="Maximum events to return")


@tool(
    description="Get the user's upcoming meetings for the next N days. Returns subject, time, attendees, and join URL."
)
async def get_upcoming_meetings(params: GetUpcomingMeetingsInput, context: dict) -> list[dict]:
    token = context["access_token"]
    now = datetime.now(UTC)
    end = now + timedelta(days=params.days_ahead)

    data = await graph_get(
        token,
        "/me/calendarView",
        **{
            "startDateTime": now.isoformat(),
            "endDateTime": end.isoformat(),
            "$select": _SELECT_BRIEF,
            "$top": params.max_results,
            "$orderby": "start/dateTime",
        },
    )
    return [_slim_event(e) for e in (data.get("value") or [])]


@tool(
    description="Get full details of a specific calendar event including agenda, location, and online meeting link."
)
async def get_meeting_details(params: GetMeetingDetailsInput, context: dict) -> dict:
    token = context["access_token"]
    data = await graph_get(token, f"/me/events/{params.meeting_id}", **{"$select": _SELECT_EVENT})
    return _full_event(data)


@tool(description="Get the attendee list for a calendar event including their response status.")
async def get_meeting_attendees(params: GetMeetingAttendeesInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token, f"/me/events/{params.meeting_id}", **{"$select": "attendees,organizer"}
    )
    attendees = data.get("attendees") or []
    return [
        {
            "email": a.get("emailAddress", {}).get("address", ""),
            "name": a.get("emailAddress", {}).get("name", ""),
            "response": a.get("status", {}).get("response", "none"),
        }
        for a in attendees
    ]


@tool(
    description="Get calendar events within a specific date range. Useful for finding related past meetings."
)
async def get_calendar_events_range(
    params: GetCalendarEventsRangeInput, context: dict
) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me/calendarView",
        **{
            "startDateTime": f"{params.start_date}T00:00:00Z",
            "endDateTime": f"{params.end_date}T23:59:59Z",
            "$select": _SELECT_EVENT,
            "$top": params.max_results,
            "$orderby": "start/dateTime",
        },
    )
    return [_slim_event(e) for e in (data.get("value") or [])]


# ── Helpers ───────────────────────────────────────────────────────────────────


def _slim_event(e: dict) -> dict:
    return {
        "id": e.get("id", ""),
        "subject": e.get("subject", ""),
        "start": (e.get("start") or {}).get("dateTime", ""),
        "end": (e.get("end") or {}).get("dateTime", ""),
        "organizer": (e.get("organizer") or {}).get("emailAddress", {}).get("address", ""),
        "is_online": e.get("isOnlineMeeting", False),
    }


def _full_event(e: dict) -> dict:
    base = _slim_event(e)
    base.update(
        {
            "location": (e.get("location") or {}).get("displayName", ""),
            "body_preview": e.get("bodyPreview", ""),
            "join_url": (e.get("onlineMeeting") or {}).get("joinUrl", ""),
            "web_link": e.get("webLink", ""),
            "attendees": [
                {
                    "email": a.get("emailAddress", {}).get("address", ""),
                    "name": a.get("emailAddress", {}).get("name", ""),
                    "response": a.get("status", {}).get("response", "none"),
                }
                for a in (e.get("attendees") or [])
            ],
        }
    )
    return base
