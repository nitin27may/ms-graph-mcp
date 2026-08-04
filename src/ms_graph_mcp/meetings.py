"""Graph Meetings tools — transcripts, recordings, attendance reports, and past meeting discovery."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from ms_graph_mcp.client import _build_url, graph_get
from ms_graph_mcp.config import get_config
from ms_graph_mcp.tooling import tool

logger = logging.getLogger(__name__)

_SELECT_EVENT = "id,subject,start,end,organizer,attendees,isOnlineMeeting,onlineMeeting,onlineMeetingProvider,webLink"
_TRANSCRIPT_BATCH_SIZE = 5  # mirrors TRANSCRIPT_BATCH_SIZE in web/src/lib/graph/helpers.ts


class GetMeetingTranscriptInput(BaseModel):
    meeting_id: str = Field(
        description="The Teams join URL (https://teams.microsoft.com/l/meetup-join/...) or online meeting ID"
    )
    event_start: str = Field(
        "",
        description="ISO datetime of the meeting start — used for attendee transcript fallback when JoinWebUrl filter returns 403",
    )
    event_end: str = Field(
        "",
        description="ISO datetime of the meeting end — used for attendee transcript fallback when JoinWebUrl filter returns 403",
    )


class GetAttendanceReportInput(BaseModel):
    meeting_id: str = Field(description="The online meeting ID")


class GetPastMeetingsInput(BaseModel):
    days_back: int = Field(7, description="Number of days back to look (1–90)")
    max_results: int = Field(20, description="Maximum meetings to return")
    online_only: bool = Field(
        True, description="If True, only return meetings with a Teams join URL"
    )


class GetOnlineMeetingFromEventInput(BaseModel):
    join_url: str = Field(
        description="The Teams join URL from the calendar event (onlineMeeting.joinUrl)"
    )


class ListMeetingTranscriptsInput(BaseModel):
    meeting_id: str = Field(
        description="The online meeting ID (from get_online_meeting_from_event)"
    )


class GetMeetingsWithTranscriptsInput(BaseModel):
    days_back: int = Field(10, description="Number of days back to look (1–90)")
    max_meetings: int = Field(
        50,
        description="Maximum meetings to return (transcript check is concurrent so larger values are fine)",
    )


class GetTranscriptByEventIdInput(BaseModel):
    event_id: str = Field(
        "", description="The calendar event ID (from a meeting card or get_meeting_details)"
    )
    join_url: str = Field(
        "", description="Optional: Teams join URL if already known — skips the event lookup step"
    )
    online_meeting_id: str = Field(
        "",
        description="Optional: Teams online meeting ID if already known — skips both the event lookup and JoinWebUrl filter steps entirely. Use this when the meeting card already includes online_meeting_id.",
    )


async def _resolve_attendee_transcript(
    token: str,
    headers: dict,
    event_start: str,
    event_end: str,
) -> str | None:
    """
    Attendee fallback: call getAllTranscripts and find the meeting ID via time-based match.
    Returns the online meeting ID if found, else None.
    """
    import httpx

    if not event_start:
        return None

    def _parse_dt(s: str) -> datetime:
        s = s.rstrip("Z")
        if "." in s:
            s = s[:26]
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    try:
        ev_start_dt = _parse_dt(event_start)
        ev_end_dt = _parse_dt(event_end) if event_end else ev_start_dt + timedelta(hours=4)
        search_start = (ev_start_dt - timedelta(hours=1)).isoformat()
        async with httpx.AsyncClient(verify=not get_config().disable_ssl_verify, timeout=30) as client:
            resp = await client.get(
                _build_url(
                    "https://graph.microsoft.com/v1.0/me/onlineMeetings/getAllTranscripts",
                    **{
                        "$filter": f"startDateTime ge '{search_start}'",
                        "$select": "id,createdDateTime,meetingId",
                        "$top": 50,
                    },
                ),
                headers=headers,
            )
        if not resp.is_success:
            logger.warning(
                "_resolve_attendee_transcript: getAllTranscripts HTTP %s", resp.status_code
            )
            return None
        window_end = ev_end_dt + timedelta(hours=3)
        for tr in resp.json().get("value") or []:
            created = tr.get("createdDateTime", "")
            if not created:
                continue
            try:
                tr_dt = _parse_dt(created)
                if ev_start_dt <= tr_dt <= window_end and tr.get("meetingId"):
                    logger.info(
                        "_resolve_attendee_transcript: matched meetingId=%s", tr["meetingId"]
                    )
                    return tr["meetingId"]
            except Exception:
                continue
    except Exception as exc:
        logger.warning("_resolve_attendee_transcript: failed: %s", exc)
    return None


@tool(
    description="Get the transcript of an online meeting. Accepts either an online meeting ID or a Teams join URL (https://teams.microsoft.com/l/meetup-join/...). Returns time-stamped speaker turns with spoken text."
)
async def get_meeting_transcript(params: GetMeetingTranscriptInput, context: dict) -> dict:
    import httpx

    token = context["access_token"]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # Resolve the meeting_id to an actual online meeting ID.
    # Three input forms are accepted:
    #  1. Teams join URL (https://teams.microsoft.com/l/...) → resolve via JoinWebUrl filter
    #  2. Calendar event ID (AAMk..., AQMk...) → fetch event to extract joinUrl then resolve
    #  3. Online meeting ID (already correct, e.g. MSp...) → use directly
    actual_id = params.meeting_id
    if params.meeting_id.startswith("https://"):
        join_url = params.meeting_id
        odata_safe_url = join_url.replace("'", "''")
        async with httpx.AsyncClient(verify=not get_config().disable_ssl_verify, timeout=20) as client:
            resp = await client.get(
                _build_url(
                    "https://graph.microsoft.com/v1.0/me/onlineMeetings",
                    **{"$filter": f"JoinWebUrl eq '{odata_safe_url}'", "$select": "id"},
                ),
                headers=headers,
            )
        if resp.status_code == 403:
            # Attendee meeting — JoinWebUrl filter is organizer-only.
            # Fall back to getAllTranscripts + time-based match.
            logger.info(
                "get_meeting_transcript: 403 on JoinWebUrl, trying getAllTranscripts fallback for %s",
                join_url[:60],
            )
            actual_id = await _resolve_attendee_transcript(
                token, headers, params.event_start, params.event_end
            )
            if not actual_id:
                return {
                    "transcript": "",
                    "segments": [],
                    "error": "attendee_access_only",
                    "message": "Transcript requires meeting organizer role. No time-matched transcript found via getAllTranscripts.",
                }
        elif resp.is_success:
            matches = resp.json().get("value") or []
            if not matches:
                logger.warning(
                    "get_meeting_transcript: no online meeting found for joinUrl=%s", join_url
                )
                return {
                    "transcript": "",
                    "segments": [],
                    "error": "no_online_meeting_found_for_join_url",
                }
            actual_id = matches[0]["id"]
        else:
            return {
                "transcript": "",
                "segments": [],
                "error": f"onlineMeetings filter HTTP {resp.status_code}",
            }
    elif params.meeting_id and not params.meeting_id.startswith("MSp"):
        # Looks like a calendar event ID — extract joinUrl from the event
        try:
            event = await graph_get(
                token,
                f"/me/events/{params.meeting_id}",
                **{"$select": "onlineMeeting"},
            )
            join_url = (event.get("onlineMeeting") or {}).get("joinUrl")
            if join_url:
                odata_safe_url = join_url.replace("'", "''")
                async with httpx.AsyncClient(
                    verify=not get_config().disable_ssl_verify, timeout=20
                ) as client:
                    resp = await client.get(
                        _build_url(
                            "https://graph.microsoft.com/v1.0/me/onlineMeetings",
                            **{"$filter": f"JoinWebUrl eq '{odata_safe_url}'", "$select": "id"},
                        ),
                        headers=headers,
                    )
                if resp.status_code == 403:
                    actual_id = await _resolve_attendee_transcript(
                        token, headers, params.event_start, params.event_end
                    )
                    if not actual_id:
                        return {"transcript": "", "segments": [], "error": "attendee_access_only"}
                elif resp.is_success:
                    matches = resp.json().get("value") or []
                    if matches:
                        actual_id = matches[0]["id"]
        except Exception as exc:
            logger.warning(
                "get_meeting_transcript: calendar event lookup failed for %s: %s",
                params.meeting_id,
                exc,
            )

    # Fetch transcript metadata list
    transcripts = await graph_get(
        token,
        f"/me/onlineMeetings/{actual_id}/transcripts",
    )
    items = transcripts.get("value") or []
    if not items:
        return {"transcript": "", "segments": []}

    # Take the most recent transcript
    transcript_id = items[-1]["id"]

    # Fetch transcript content as VTT text
    import httpx

    url = f"https://graph.microsoft.com/v1.0/me/onlineMeetings/{actual_id}/transcripts/{transcript_id}/content?$format=text/vtt"
    async with httpx.AsyncClient(verify=not get_config().disable_ssl_verify, timeout=30) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 200:
            vtt_text = resp.text
        else:
            vtt_text = ""

    parsed = _parse_vtt(vtt_text)
    _MAX_TRANSCRIPT = 100000
    return {
        "transcript_id": transcript_id,
        "transcript": parsed[:_MAX_TRANSCRIPT],
        "transcript_length": len(parsed),
        "truncated": len(parsed) > _MAX_TRANSCRIPT,
        "raw_vtt": vtt_text[:3000],
    }


@tool(
    description="Get past meetings from the last N days. Optionally filter to online meetings only. Useful for finding meetings to summarize."
)
async def get_past_meetings(params: GetPastMeetingsInput, context: dict) -> list[dict]:
    token = context["access_token"]
    now = datetime.now(UTC)
    start = now - timedelta(days=params.days_back)
    # Extend end to midnight tonight so today's remaining meetings are included
    end_of_today = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    import httpx

    async with httpx.AsyncClient(verify=not get_config().disable_ssl_verify, timeout=30) as client:
        resp = await client.get(
            _build_url(
                "https://graph.microsoft.com/v1.0/me/calendarView",
                startDateTime=start.isoformat(),
                endDateTime=end_of_today.isoformat(),
                **{
                    "$select": _SELECT_EVENT,
                    "$top": min(params.max_results, 50),
                    "$orderby": "start/dateTime desc",
                },
            ),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if not resp.is_success:
            return []
        data = resp.json()

    events = data.get("value") or []
    results = []
    for e in events:
        is_online = e.get("isOnlineMeeting", False)
        join_url = (e.get("onlineMeeting") or {}).get("joinUrl", "")
        if params.online_only and not join_url:
            continue
        results.append(
            {
                "id": e.get("id", ""),
                "subject": e.get("subject", ""),
                "start": (e.get("start") or {}).get("dateTime", ""),
                "end": (e.get("end") or {}).get("dateTime", ""),
                "organizer_email": (e.get("organizer") or {})
                .get("emailAddress", {})
                .get("address", ""),
                "organizer_name": (e.get("organizer") or {})
                .get("emailAddress", {})
                .get("name", ""),
                "attendees": [
                    {
                        "email": (a.get("emailAddress") or {}).get("address", ""),
                        "name": (a.get("emailAddress") or {}).get("name", ""),
                    }
                    for a in (e.get("attendees") or [])
                ],
                "is_online": is_online,
                "join_url": join_url,
                "web_link": e.get("webLink", ""),
            }
        )
    return results


@tool(
    description=(
        "Look up an online meeting object from its Teams join URL. "
        "Returns the online meeting ID needed to fetch transcripts or attendance reports. "
        "Use this after get_past_meetings to resolve calendar event → online meeting ID."
    )
)
async def get_online_meeting_from_event(
    params: GetOnlineMeetingFromEventInput, context: dict
) -> dict:
    token = context["access_token"]
    import httpx

    # Use params= so httpx percent-encodes the filter value properly.
    # Teams join URLs contain %3a, %40 etc. which must be double-encoded (%253a)
    # so the Graph API's single URL-decode step restores them before string comparison.
    # This matches the TypeScript SDK's encodeURIComponent() behaviour.
    odata_safe_url = params.join_url.replace("'", "''")
    async with httpx.AsyncClient(verify=not get_config().disable_ssl_verify, timeout=30) as client:
        resp = await client.get(
            _build_url(
                "https://graph.microsoft.com/v1.0/me/onlineMeetings",
                **{"$filter": f"JoinWebUrl eq '{odata_safe_url}'"},
            ),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if not resp.is_success:
            return {"error": f"Could not look up meeting: HTTP {resp.status_code}"}
        data = resp.json()

    items = data.get("value") or []
    if not items:
        return {"error": "No online meeting found for this join URL"}

    m = items[0]
    return {
        "id": m.get("id", ""),
        "subject": m.get("subject", ""),
        "start": m.get("startDateTime", ""),
        "end": m.get("endDateTime", ""),
        "join_url": m.get("joinWebUrl", ""),
    }


@tool(
    description=(
        "List transcript metadata for an online meeting — returns transcript IDs and creation dates. "
        "Use this to check whether a meeting has transcripts before fetching the full content."
    )
)
async def list_meeting_transcripts(
    params: ListMeetingTranscriptsInput, context: dict
) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        f"/me/onlineMeetings/{params.meeting_id}/transcripts",
        **{"$select": "id,createdDateTime,meetingId"},
    )
    items = data.get("value") or []
    return [
        {
            "transcript_id": t.get("id", ""),
            "created_at": t.get("createdDateTime", ""),
            "meeting_id": params.meeting_id,
        }
        for t in items
    ]


@tool(
    description=(
        "Get past meetings with accurate has_transcript flags. "
        "Returns ALL meetings (online and in-person) from the last N days. "
        "Uses getAllTranscripts API which works for both meeting organizers AND attendees. "
        "Use this — not get_past_meetings — whenever you will render a meeting_list card, "
        "so the Transcripts filter works correctly in the UI."
    )
)
async def get_meetings_with_transcripts(
    params: GetMeetingsWithTranscriptsInput, context: dict
) -> list[dict]:
    """
    Two-path transcript detection:
      - Organizer path: GET /me/onlineMeetings?$filter=JoinWebUrl eq '...' → /transcripts
      - Attendee path (403 fallback): GET /me/onlineMeetings/getAllTranscripts → time-based match
        (createdDateTime within [event.start, event.end + 3h])
    getAllTranscripts is pre-fetched once for the whole period before the per-event batch.
    """
    import httpx

    token = context["access_token"]
    now = datetime.now(UTC)
    start = now - timedelta(days=params.days_back)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # ── Step 1: Fetch calendar events (getUserMeetings equivalent) ────────────
    async with httpx.AsyncClient(verify=not get_config().disable_ssl_verify, timeout=30) as client:
        resp = await client.get(
            _build_url(
                "https://graph.microsoft.com/v1.0/me/calendarView",
                startDateTime=start.isoformat(),
                endDateTime=now.isoformat(),
                **{
                    "$select": _SELECT_EVENT,
                    "$top": min(params.max_meetings, 100),
                    "$orderby": "start/dateTime desc",
                },
            ),
            headers=headers,
        )
    events = resp.json().get("value") or [] if resp.is_success else []

    # Filter online meetings only (same as web: events.filter(e => e.isOnlineMeeting))
    online_events = [
        e
        for e in events
        if e.get("isOnlineMeeting") and (e.get("onlineMeeting") or {}).get("joinUrl")
    ]

    logger.info(
        "[get_meetings_with_transcripts] total_events=%d  online_events=%d",
        len(events),
        len(online_events),
    )

    # ── Step 2: Pre-fetch all transcripts via getAllTranscripts (works for attendees too) ──
    # JoinWebUrl filter returns 403 for attendee meetings — getAllTranscripts covers both.
    all_transcripts: list[dict] = []
    try:
        # Try without $filter first (some tenants reject it); just fetch recent transcripts.
        async with httpx.AsyncClient(verify=not get_config().disable_ssl_verify, timeout=30) as client:
            tr_all_resp = await client.get(
                _build_url(
                    "https://graph.microsoft.com/v1.0/me/onlineMeetings/getAllTranscripts",
                    **{"$top": 100},
                ),
                headers=headers,
            )
        if tr_all_resp.is_success:
            all_transcripts = tr_all_resp.json().get("value") or []
            logger.info(
                "[get_meetings_with_transcripts] getAllTranscripts returned %d items",
                len(all_transcripts),
            )
        else:
            try:
                err_body = tr_all_resp.json().get("error", {})
                logger.warning(
                    "[get_meetings_with_transcripts] getAllTranscripts HTTP %s code=%s msg=%s",
                    tr_all_resp.status_code,
                    err_body.get("code", "?"),
                    err_body.get("message", "?")[:200],
                )
            except Exception:
                logger.warning(
                    "[get_meetings_with_transcripts] getAllTranscripts HTTP %s body=%s",
                    tr_all_resp.status_code,
                    tr_all_resp.text[:300],
                )
    except Exception as exc:
        logger.warning(
            "[get_meetings_with_transcripts] getAllTranscripts failed: %s — attendee fallback unavailable",
            exc,
        )

    def _find_transcript_by_time(
        event_start_str: str, event_end_str: str
    ) -> tuple[str | None, bool]:
        """
        Find a transcript where createdDateTime falls within [event_start, event_end + 3h].
        Returns (online_meeting_id, has_transcript).
        Used as an attendee fallback when JoinWebUrl filter returns 403.
        The +3h window accounts for meetings that run over or where recording starts late.
        """
        if not all_transcripts:
            return None, False
        try:

            def _parse_dt(s: str) -> datetime:
                s = s.rstrip("Z")
                if "." in s:
                    s = s[:26]
                dt = datetime.fromisoformat(s)
                return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

            ev_start = _parse_dt(event_start_str)
            ev_end = _parse_dt(event_end_str)
            window_end = ev_end + timedelta(hours=3)

            for tr in all_transcripts:
                created = tr.get("createdDateTime", "")
                if not created:
                    continue
                try:
                    tr_dt = _parse_dt(created)
                    if ev_start <= tr_dt <= window_end:
                        meeting_id = tr.get("meetingId") or None
                        logger.info(
                            "[find_transcript_by_time] matched transcript meetingId=%s for window %s–%s",
                            meeting_id,
                            event_start_str[:19],
                            event_end_str[:19],
                        )
                        return meeting_id, True
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("[find_transcript_by_time] failed: %s", exc)
        return None, False

    # ── Step 3: Resolve transcript per event — organizer path with attendee fallback ──
    async def _resolve_transcript(event: dict) -> tuple[str | None, bool]:
        """
        Primary: JoinWebUrl filter (works for meeting organizers).
        Fallback on 403: time-based match against getAllTranscripts (works for attendees).
        Returns (online_meeting_id, has_transcript).
        """
        join_url = (event.get("onlineMeeting") or {}).get("joinUrl", "")
        if not join_url:
            return None, False
        try:
            odata_safe_url = join_url.replace("'", "''")
            async with httpx.AsyncClient(
                verify=not get_config().disable_ssl_verify, timeout=20
            ) as client:
                om_resp = await client.get(
                    _build_url(
                        "https://graph.microsoft.com/v1.0/me/onlineMeetings",
                        **{"$filter": f"JoinWebUrl eq '{odata_safe_url}'"},
                    ),
                    headers=headers,
                )

            if om_resp.status_code == 403:
                # Attendee meeting — JoinWebUrl filter is organizer-only
                logger.debug(
                    "[resolve_transcript] 403 on JoinWebUrl for %s, trying time-based match",
                    join_url[:60],
                )
                ev_start = (event.get("start") or {}).get("dateTime", "")
                ev_end = (event.get("end") or {}).get("dateTime", "")
                return _find_transcript_by_time(ev_start, ev_end)

            if not om_resp.is_success:
                try:
                    err = om_resp.json().get("error", {})
                    logger.warning(
                        "[resolve_transcript] HTTP %s code=%s msg=%s url=%s",
                        om_resp.status_code,
                        err.get("code", "?"),
                        err.get("message", "?")[:200],
                        str(om_resp.request.url)[:200],
                    )
                except Exception:
                    logger.warning(
                        "[resolve_transcript] HTTP %s body=%s",
                        om_resp.status_code,
                        om_resp.text[:300],
                    )
                return None, False

            items = om_resp.json().get("value") or []
            if not items:
                return None, False

            om_id = items[0]["id"]
            async with httpx.AsyncClient(
                verify=not get_config().disable_ssl_verify, timeout=20
            ) as client:
                tr_resp = await client.get(
                    f"https://graph.microsoft.com/v1.0/me/onlineMeetings/{om_id}/transcripts?$select=id",
                    headers=headers,
                )
            has_transcript = tr_resp.is_success and bool(tr_resp.json().get("value"))
            return om_id, has_transcript

        except Exception as exc:
            logger.debug("[resolve_transcript] exception: %s", exc)
            return None, False

    # ── Step 4: Batched concurrent transcript resolution ─────────────────────
    transcript_map: dict[str, tuple[str | None, bool]] = {}  # event_id → (om_id, has_transcript)

    for batch_start in range(0, len(online_events), _TRANSCRIPT_BATCH_SIZE):
        batch = online_events[batch_start : batch_start + _TRANSCRIPT_BATCH_SIZE]
        results = await asyncio.gather(*[_resolve_transcript(e) for e in batch])
        for event, result in zip(batch, results):
            transcript_map[event["id"]] = result

    # ── Step 5: Build final meeting list ─────────────────────────────────────
    def _build_meeting(e: dict) -> dict:
        join_url = (e.get("onlineMeeting") or {}).get("joinUrl", "")
        event_id = e.get("id", "")
        om_id, has_transcript = transcript_map.get(event_id, (None, False))

        logger.info(
            "[get_meetings_with_transcripts] '%s'  has_transcript=%s",
            e.get("subject", "?"),
            has_transcript,
        )
        return {
            "event_id": event_id,
            "subject": e.get("subject", ""),
            "start": (e.get("start") or {}).get("dateTime", ""),
            "end": (e.get("end") or {}).get("dateTime", ""),
            "organizer": (e.get("organizer") or {}).get("emailAddress", {}).get("address", ""),
            "organizer_name": (e.get("organizer") or {}).get("emailAddress", {}).get("name", ""),
            "attendees": [
                {
                    "email": (a.get("emailAddress") or {}).get("address", ""),
                    "name": (a.get("emailAddress") or {}).get("name", ""),
                }
                for a in (e.get("attendees") or [])
            ],
            "is_online": True,
            "join_url": join_url,
            "location": (e.get("location") or {}).get("displayName", ""),
            "has_transcript": has_transcript,
            "online_meeting_id": om_id,
        }

    return [_build_meeting(e) for e in online_events]


@tool(
    description=(
        "Get the full transcript for a meeting. "
        "Pass online_meeting_id if available (fastest — skips all lookups). "
        "Pass join_url to skip the event fetch. Pass event_id as a fallback. "
        "Handles the full pipeline: event → join URL → online meeting ID → transcript content. "
        "Always prefer passing online_meeting_id when the meeting card already includes it."
    )
)
async def get_transcript_by_event_id(params: GetTranscriptByEventIdInput, context: dict) -> dict:
    import httpx

    token = context["access_token"]
    subject = ""
    online_meeting_id = params.online_meeting_id

    if not online_meeting_id:
        # Step 1: Resolve join URL from event if not provided
        join_url = params.join_url
        if not join_url:
            if not params.event_id:
                return {"error": "Must provide event_id, join_url, or online_meeting_id."}
            event_data = await graph_get(
                token,
                f"/me/events/{params.event_id}",
                **{"$select": "id,subject,isOnlineMeeting,onlineMeeting"},
            )
            subject = event_data.get("subject", "")
            join_url = (event_data.get("onlineMeeting") or {}).get("joinUrl", "")
            if not join_url:
                return {
                    "error": "This meeting does not have a Teams join URL — no transcript available.",
                    "subject": subject,
                }

        # Step 2: Resolve online meeting ID using JoinWebUrl filter (organizer path).
        # On 403 (attendee), fall back to getAllTranscripts + time-based match.
        odata_safe_url = join_url.replace("'", "''")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        async with httpx.AsyncClient(verify=not get_config().disable_ssl_verify, timeout=30) as client:
            resp = await client.get(
                _build_url(
                    "https://graph.microsoft.com/v1.0/me/onlineMeetings",
                    **{"$filter": f"JoinWebUrl eq '{odata_safe_url}'"},
                ),
                headers=headers,
            )

        if resp.status_code == 403:
            # Attendee meeting — try getAllTranscripts + time-based match to find the online_meeting_id
            logger.info(
                "[get_transcript_by_event_id] 403 on JoinWebUrl, trying getAllTranscripts fallback for event %s",
                params.event_id,
            )
            if params.event_id:
                event_data_full = await graph_get(
                    token,
                    f"/me/events/{params.event_id}",
                    **{"$select": "id,subject,start,end,isOnlineMeeting"},
                )
                ev_start_str = (event_data_full.get("start") or {}).get("dateTime", "")
                ev_end_str = (event_data_full.get("end") or {}).get("dateTime", "")
                if not subject:
                    subject = event_data_full.get("subject", "")
            else:
                ev_start_str = ev_end_str = ""

            if ev_start_str:
                try:
                    ev_start_dt = datetime.fromisoformat(ev_start_str.rstrip("Z")).replace(
                        tzinfo=UTC
                    )
                    search_start = (ev_start_dt - timedelta(days=1)).isoformat()
                    async with httpx.AsyncClient(
                        verify=not get_config().disable_ssl_verify, timeout=30
                    ) as client:
                        all_tr_resp = await client.get(
                            _build_url(
                                "https://graph.microsoft.com/v1.0/me/onlineMeetings/getAllTranscripts",
                                **{
                                    "$filter": f"startDateTime ge '{search_start}'",
                                    "$select": "id,createdDateTime,meetingId",
                                    "$top": 50,
                                },
                            ),
                            headers=headers,
                        )
                    if all_tr_resp.is_success:

                        def _parse_dt2(s: str) -> datetime:
                            s = s.rstrip("Z")
                            if "." in s:
                                s = s[:26]
                            dt = datetime.fromisoformat(s)
                            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

                        ev_start_dt2 = _parse_dt2(ev_start_str)
                        ev_end_dt2 = (
                            _parse_dt2(ev_end_str)
                            if ev_end_str
                            else ev_start_dt2 + timedelta(hours=4)
                        )
                        window_end = ev_end_dt2 + timedelta(hours=3)
                        for tr in all_tr_resp.json().get("value") or []:
                            created = tr.get("createdDateTime", "")
                            if not created:
                                continue
                            try:
                                tr_dt = _parse_dt2(created)
                                if ev_start_dt2 <= tr_dt <= window_end and tr.get("meetingId"):
                                    online_meeting_id = tr["meetingId"]
                                    logger.info(
                                        "[get_transcript_by_event_id] attendee fallback matched meetingId=%s",
                                        online_meeting_id,
                                    )
                                    break
                            except Exception:
                                continue
                except Exception as exc:
                    logger.warning(
                        "[get_transcript_by_event_id] getAllTranscripts fallback failed: %s", exc
                    )

            if not online_meeting_id:
                return {
                    "error": "Could not retrieve transcript. You appear to be an attendee (not organizer) and no transcript was found in the time window.",
                    "subject": subject,
                }

        elif not resp.is_success:
            return {
                "error": f"Could not look up online meeting (HTTP {resp.status_code}).",
                "subject": subject,
                "debug_status": resp.status_code,
            }
        else:
            items = resp.json().get("value") or []
            if not items:
                return {
                    "error": "No online meeting record found. The meeting may not have been a Teams meeting or transcript was not enabled.",
                    "subject": subject,
                }
            online_meeting_id = items[0]["id"]
            if not subject:
                subject = items[0].get("subject", "")

    # Step 3: List transcripts
    async with httpx.AsyncClient(verify=not get_config().disable_ssl_verify, timeout=30) as client:
        tr_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/me/onlineMeetings/{online_meeting_id}/transcripts",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    if not tr_resp.is_success:
        return {
            "error": f"Could not list transcripts (HTTP {tr_resp.status_code}).",
            "subject": subject,
            "online_meeting_id": online_meeting_id,
        }

    transcripts = tr_resp.json().get("value") or []
    if not transcripts:
        return {
            "transcript": "",
            "subject": subject,
            "online_meeting_id": online_meeting_id,
            "message": "No transcript found for this meeting. Recording may not have been enabled.",
        }

    # Step 4: Fetch latest transcript content as VTT
    latest = transcripts[-1]["id"]
    async with httpx.AsyncClient(verify=not get_config().disable_ssl_verify, timeout=60) as client:
        content_resp = await client.get(
            f"https://graph.microsoft.com/v1.0/me/onlineMeetings/{online_meeting_id}/transcripts/{latest}/content?$format=text/vtt",
            headers={"Authorization": f"Bearer {token}"},
        )

    vtt = content_resp.text if content_resp.status_code == 200 else ""
    # Return the raw VTT as the transcript — the frontend's parseTranscript
    # knows how to handle ``WEBVTT … <v Speaker>text</v>`` cues and renders
    # them as a speaker-annotated list. Stripping to plain text (the old
    # behaviour) erased all speaker tags and collapsed to a single line,
    # leaving the Transcript tab visually blank. ``_parse_vtt`` is still
    # used elsewhere (for flat-text contexts); only this return is changed.
    # GPT-4.1 also handles VTT fine as summarizer input.
    _MAX_TRANSCRIPT = 100000
    truncated = len(vtt) > _MAX_TRANSCRIPT
    return {
        "online_meeting_id": online_meeting_id,
        "subject": subject,
        "transcript_id": latest,
        "transcript": vtt[:_MAX_TRANSCRIPT],
        "transcript_length": len(vtt),
        "truncated": truncated,
        "raw_vtt": vtt[:3000],
    }


@tool(
    description="Get the attendance report for an online meeting — who attended, join/leave times, duration."
)
async def get_attendance_report(params: GetAttendanceReportInput, context: dict) -> dict:
    token = context["access_token"]
    data = await graph_get(
        token,
        f"/me/onlineMeetings/{params.meeting_id}/attendanceReports",
    )
    reports = data.get("value") or []
    if not reports:
        return {"attendees": [], "total_attendees": 0}

    # Most recent report
    report_id = reports[-1]["id"]
    detail = await graph_get(
        token,
        f"/me/onlineMeetings/{params.meeting_id}/attendanceReports/{report_id}/attendanceRecords",
        **{"$select": "emailAddress,attendanceIntervals,role,totalAttendanceInSeconds"},
    )
    records = detail.get("value") or []
    return {
        "total_attendees": len(records),
        "attendees": [
            {
                "email": r.get("emailAddress", ""),
                "role": r.get("role", "attendee"),
                "duration_seconds": r.get("totalAttendanceInSeconds", 0),
                "intervals": r.get("attendanceIntervals") or [],
            }
            for r in records
        ],
    }


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_vtt(vtt: str) -> str:
    """Strip VTT timing cues, return plain speaker text."""
    if not vtt:
        return ""
    lines = []
    for line in vtt.splitlines():
        line = line.strip()
        if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
            continue
        lines.append(line)
    return " ".join(lines)
