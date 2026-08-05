"""
Tests for ms_graph_mcp.meetings

Run from the backend/ directory:
    uv run pytest tests/test_graph_meetings.py -v

Key areas tested:
1. JoinWebUrl OData filter encoding — the core bug fix
2. meetings_list_with_transcripts — full pipeline with mocked HTTP
3. meetings_get_transcript_by_event — all three code paths
4. _parse_vtt — VTT strip helper
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

# A realistic Teams join URL containing percent-encoded characters (%3a, %40, %7b, %22, %7d)
SAMPLE_JOIN_URL = (
    "https://teams.microsoft.com/l/meetup-join/"
    "19%3ameeting_OTQ5YjgxN2ItZDdkZC00YWI5LTliNzMtOTQ0N2U1NmQ4NjFj%40thread.v2/"
    "0?context=%7b%22Tid%22%3a%22tenant-id-1234%22%2c%22Oid%22%3a%22user-id-5678%22%7d"
)

SAMPLE_ONLINE_MEETING_ID = "MSo1N2Y5OGM3OS1hNGY2LTQ0NzItODQxMC1hMzNiYTQxMmVkNTE="
SAMPLE_TRANSCRIPT_ID = "VjEjMCMwMzBlOGQ5OC0xMWUzLTRlZjEtOTJkNC0yNTZlOTliOWE5ZjM="
SAMPLE_EVENT_ID = "AAMkAGE4YWNiMGIzLWZhNTktNGY5Ni1iOTJlLWUzOGM0ODBhMDI0MABGAAAAAADEzM"

TOKEN = "fake-access-token"
CONTEXT = {"access_token": TOKEN}

AUTH_HEADER = f"Bearer {TOKEN}"


def _make_response(status: int, body: dict | str) -> MagicMock:
    """Build a MagicMock that looks like an httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.is_success = 200 <= status < 300
    # A real httpx.Response always carries headers; graph_try_get reads
    # content-type off them to decide how to hand the body back.
    resp.headers = {"content-type": "application/json"}
    if isinstance(body, dict):
        resp.json.return_value = body
        resp.text = json.dumps(body)
    else:
        resp.json.side_effect = ValueError("not json")
        resp.text = body
    return resp


# ── 1. URL encoding correctness ───────────────────────────────────────────────


class TestJoinUrlEncoding:
    """
    Verify that the OData filter value is correctly encoded when passed via
    httpx params=.  Teams join URLs contain %3a, %40, %7b etc. which must be
    double-encoded (%253a etc.) so the Graph API's single URL-decode step
    recovers the original percent-encoded chars before string comparison.
    """

    def _build_request(self, join_url: str) -> httpx.Request:
        """Build an httpx.Request as graph_meetings would, using params=."""
        odata_safe = join_url.replace("'", "''")
        return httpx.Request(
            "GET",
            "https://graph.microsoft.com/v1.0/me/onlineMeetings",
            params={"$filter": f"JoinWebUrl eq '{odata_safe}'", "$select": "id"},
            headers={"Authorization": AUTH_HEADER},
        )

    def test_percent_sign_is_double_encoded(self):
        """
        % in the join URL must be encoded as %25 so that after one URL-decode
        the server recovers the original percent-encoded sequence.
        e.g. %3a → %253a in the request → %3a after server decodes → matches stored value.
        """
        req = self._build_request(SAMPLE_JOIN_URL)
        raw_query = req.url.query.decode()
        # %3a in the join URL must appear as %253a (double-encoded) in the request
        assert "%253a" in raw_query.lower(), (
            f"Expected %253a (double-encoded %3a) in query string, got: {raw_query[:300]}"
        )

    def test_at_sign_is_double_encoded(self):
        """%40 (@ symbol) in thread ID must be double-encoded."""
        req = self._build_request(SAMPLE_JOIN_URL)
        raw_query = req.url.query.decode()
        assert "%2540" in raw_query.lower(), (
            f"Expected %2540 (double-encoded %40) in query string, got: {raw_query[:300]}"
        )

    def test_single_quote_odata_escape(self):
        """A single quote in the join URL is OData-escaped to '' before encoding."""
        url_with_quote = "https://teams.microsoft.com/l/meetup-join/test'room/0"
        req = self._build_request(url_with_quote)
        raw_query = req.url.query.decode()
        # Single quote → '' (OData escape) → %27%27 in URL-encoded form
        assert "test" in raw_query  # URL is present
        # The doubled single quote should be encoded
        assert "%27%27" in raw_query or "''".replace("'", "%27") in raw_query

    def test_filter_key_present(self):
        """The $filter key must appear in the query string (encoded or raw)."""
        req = self._build_request(SAMPLE_JOIN_URL)
        raw_query = req.url.query.decode().lower()
        # httpx may encode $ as %24 — both are valid
        assert "filter" in raw_query, f"filter key missing from: {raw_query[:300]}"

    def test_select_key_present(self):
        req = self._build_request(SAMPLE_JOIN_URL)
        raw_query = req.url.query.decode().lower()
        assert "select" in raw_query

    def test_roundtrip_restores_original(self):
        """
        Simulate what the Graph API does: URL-decode the encoded query string once.
        The result should contain the ORIGINAL join URL with %3a intact.
        """
        from urllib.parse import unquote

        req = self._build_request(SAMPLE_JOIN_URL)
        raw_query = req.url.query.decode()
        # Decode once (simulate server URL-decode)
        decoded = unquote(raw_query)
        # The original %3a should appear in the decoded filter
        assert "%3a" in decoded.lower(), (
            f"After one URL-decode, %3a should be present. Got: {decoded[:300]}"
        )


# ── 2. meetings_list_with_transcripts ─────────────────────────────────────────


def _calendar_event(
    event_id: str,
    subject: str,
    join_url: str = SAMPLE_JOIN_URL,
    is_online: bool = True,
) -> dict:
    return {
        "id": event_id,
        "subject": subject,
        "start": {"dateTime": "2026-04-01T10:00:00"},
        "end": {"dateTime": "2026-04-01T11:00:00"},
        "isOnlineMeeting": is_online,
        "onlineMeeting": {"joinUrl": join_url} if is_online else None,
        "organizer": {"emailAddress": {"address": "organizer@contoso.com", "name": "Organizer"}},
        "attendees": [
            {"emailAddress": {"address": "alice@contoso.com", "name": "Alice"}},
            {"emailAddress": {"address": "bob@contoso.com", "name": "Bob"}},
        ],
        "location": {"displayName": "Online"},
        "onlineMeetingProvider": "teamsForBusiness",
        "webLink": "https://outlook.office365.com/calendar/item/123",
    }


@pytest.mark.asyncio
class TestGetMeetingsWithTranscripts:
    async def test_meeting_with_transcript_returns_has_transcript_true(self):
        """When the online meeting has transcripts, has_transcript must be True."""
        from ms_graph_mcp.meetings import (
            GetMeetingsWithTranscriptsInput,
            meetings_list_with_transcripts,
        )

        calendar_resp = _make_response(200, {"value": [_calendar_event("evt-1", "Weekly Sync")]})
        om_resp = _make_response(200, {"value": [{"id": SAMPLE_ONLINE_MEETING_ID}]})
        transcript_resp = _make_response(200, {"value": [{"id": SAMPLE_TRANSCRIPT_ID}]})

        call_count = 0

        async def fake_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            url_str = str(url)
            if "calendarView" in url_str:
                return calendar_resp
            if "onlineMeetings" in url_str and "transcripts" not in url_str:
                return om_resp
            if "transcripts" in url_str:
                return transcript_resp
            raise ValueError(f"Unexpected URL: {url_str}")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = fake_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await meetings_list_with_transcripts(
                GetMeetingsWithTranscriptsInput(days_back=7, max_meetings=10),
                CONTEXT,
            )

        assert len(result) == 1
        meeting = result[0]
        assert meeting["has_transcript"] is True
        assert meeting["online_meeting_id"] == SAMPLE_ONLINE_MEETING_ID
        assert meeting["subject"] == "Weekly Sync"
        assert meeting["event_id"] == "evt-1"
        assert meeting["is_online"] is True

    async def test_meeting_without_transcript_returns_has_transcript_false(self):
        """When the transcript endpoint returns empty, has_transcript must be False."""
        from ms_graph_mcp.meetings import (
            GetMeetingsWithTranscriptsInput,
            meetings_list_with_transcripts,
        )

        calendar_resp = _make_response(
            200, {"value": [_calendar_event("evt-2", "No Transcript Meeting")]}
        )
        om_resp = _make_response(200, {"value": [{"id": SAMPLE_ONLINE_MEETING_ID}]})
        transcript_resp = _make_response(200, {"value": []})  # no transcripts

        async def fake_get(url, **kwargs):
            url_str = str(url)
            if "calendarView" in url_str:
                return calendar_resp
            if "onlineMeetings" in url_str and "transcripts" not in url_str:
                return om_resp
            if "transcripts" in url_str:
                return transcript_resp
            raise ValueError(f"Unexpected URL: {url_str}")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = fake_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await meetings_list_with_transcripts(
                GetMeetingsWithTranscriptsInput(days_back=7, max_meetings=10),
                CONTEXT,
            )

        assert len(result) == 1
        assert result[0]["has_transcript"] is False
        assert result[0]["online_meeting_id"] == SAMPLE_ONLINE_MEETING_ID

    async def test_attendee_meeting_403_returns_has_transcript_false(self):
        """
        When the user is an attendee (not organizer), the JoinWebUrl filter
        returns 403. The meeting should still appear but with has_transcript=False.
        This is expected behaviour, not an error.
        """
        from ms_graph_mcp.meetings import (
            GetMeetingsWithTranscriptsInput,
            meetings_list_with_transcripts,
        )

        calendar_resp = _make_response(
            200, {"value": [_calendar_event("evt-3", "Someone Else's Meeting")]}
        )
        om_403 = _make_response(
            403, {"error": {"code": "Forbidden", "message": "User is not the organizer"}}
        )

        async def fake_get(url, **kwargs):
            url_str = str(url)
            if "calendarView" in url_str:
                return calendar_resp
            if "onlineMeetings" in url_str:
                return om_403
            raise ValueError(f"Unexpected URL: {url_str}")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = fake_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await meetings_list_with_transcripts(
                GetMeetingsWithTranscriptsInput(days_back=7, max_meetings=10),
                CONTEXT,
            )

        assert len(result) == 1
        assert result[0]["has_transcript"] is False
        assert result[0]["online_meeting_id"] is None  # not resolved (403)

    async def test_non_online_events_excluded(self):
        """In-person events without a join URL are excluded from results."""
        from ms_graph_mcp.meetings import (
            GetMeetingsWithTranscriptsInput,
            meetings_list_with_transcripts,
        )

        in_person = _calendar_event("evt-4", "In-Person Standup", is_online=False)
        online = _calendar_event("evt-5", "Online Review")
        calendar_resp = _make_response(200, {"value": [in_person, online]})
        om_resp = _make_response(200, {"value": [{"id": SAMPLE_ONLINE_MEETING_ID}]})
        transcript_resp = _make_response(200, {"value": []})

        async def fake_get(url, **kwargs):
            url_str = str(url)
            if "calendarView" in url_str:
                return calendar_resp
            if "onlineMeetings" in url_str and "transcripts" not in url_str:
                return om_resp
            if "transcripts" in url_str:
                return transcript_resp
            raise ValueError(f"Unexpected URL: {url_str}")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = fake_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await meetings_list_with_transcripts(
                GetMeetingsWithTranscriptsInput(days_back=7, max_meetings=10),
                CONTEXT,
            )

        assert len(result) == 1
        assert result[0]["event_id"] == "evt-5"

    async def test_calendar_api_failure_returns_empty(self):
        """When the calendar API fails, return an empty list (no crash)."""
        from ms_graph_mcp.meetings import (
            GetMeetingsWithTranscriptsInput,
            meetings_list_with_transcripts,
        )

        async def fake_get(url, **kwargs):
            return _make_response(500, {"error": {"code": "ServiceError"}})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = fake_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await meetings_list_with_transcripts(
                GetMeetingsWithTranscriptsInput(days_back=7, max_meetings=10),
                CONTEXT,
            )

        assert result == []


# ── 3. meetings_get_transcript_by_event ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestGetTranscriptByEventId:
    async def test_with_online_meeting_id_skips_lookup(self):
        """
        When online_meeting_id is provided directly, both the event fetch
        and the JoinWebUrl lookup are skipped (fastest path).
        """
        from ms_graph_mcp.meetings import (
            GetTranscriptByEventIdInput,
            meetings_get_transcript_by_event,
        )

        vtt_content = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:03.000\nAlice: Hello world."
        transcript_list_resp = _make_response(200, {"value": [{"id": SAMPLE_TRANSCRIPT_ID}]})
        content_resp = MagicMock(spec=httpx.Response)
        content_resp.status_code = 200
        content_resp.text = vtt_content
        content_resp.is_success = True
        content_resp.headers = {"content-type": "text/vtt"}

        async def fake_get(url, **kwargs):
            url_str = str(url)
            if "transcripts" in url_str and "content" in url_str:
                return content_resp
            if "transcripts" in url_str:
                return transcript_list_resp
            raise ValueError(f"Unexpected URL in fast-path test: {url_str}")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = fake_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await meetings_get_transcript_by_event(
                GetTranscriptByEventIdInput(online_meeting_id=SAMPLE_ONLINE_MEETING_ID),
                CONTEXT,
            )

        assert "error" not in result
        assert result["online_meeting_id"] == SAMPLE_ONLINE_MEETING_ID
        assert result["transcript_id"] == SAMPLE_TRANSCRIPT_ID
        assert "Alice" in result["transcript"]

    async def test_event_id_path_resolves_join_url(self):
        """When only event_id is provided, the pipeline fetches event → join URL → OM ID."""
        from unittest.mock import AsyncMock as AM

        from ms_graph_mcp.meetings import (
            GetTranscriptByEventIdInput,
            meetings_get_transcript_by_event,
        )

        event_resp_data = {
            "id": SAMPLE_EVENT_ID,
            "subject": "Project Kickoff",
            "isOnlineMeeting": True,
            "onlineMeeting": {"joinUrl": SAMPLE_JOIN_URL},
        }
        om_resp_data = {"value": [{"id": SAMPLE_ONLINE_MEETING_ID, "subject": "Project Kickoff"}]}
        transcript_list_data = {"value": [{"id": SAMPLE_TRANSCRIPT_ID}]}
        vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nSpeaker: Test content."

        # graph_get is used for the event lookup; httpx directly for the rest
        with patch("ms_graph_mcp.meetings.graph_get", new=AM(return_value=event_resp_data)):

            async def fake_get(url, **kwargs):
                url_str = str(url)
                if "onlineMeetings" in url_str and "transcripts" not in url_str:
                    return _make_response(200, om_resp_data)
                if "transcripts" in url_str and "content" in url_str:
                    content = MagicMock(spec=httpx.Response)
                    content.status_code = 200
                    content.text = vtt
                    content.is_success = True
                    content.headers = {"content-type": "text/vtt"}
                    return content
                if "transcripts" in url_str:
                    return _make_response(200, transcript_list_data)
                raise ValueError(f"Unexpected URL: {url_str}")

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = fake_get
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await meetings_get_transcript_by_event(
                    GetTranscriptByEventIdInput(event_id=SAMPLE_EVENT_ID),
                    CONTEXT,
                )

        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        assert result["subject"] == "Project Kickoff"
        assert "Test content" in result["transcript"]

    async def test_attendee_meeting_returns_descriptive_error(self):
        """When the user is an attendee and JoinWebUrl lookup returns 403, a clear error is returned."""
        from unittest.mock import AsyncMock as AM

        from ms_graph_mcp.meetings import (
            GetTranscriptByEventIdInput,
            meetings_get_transcript_by_event,
        )

        event_data = {
            "subject": "Someone Else Organised",
            "isOnlineMeeting": True,
            "onlineMeeting": {"joinUrl": SAMPLE_JOIN_URL},
        }
        with patch("ms_graph_mcp.meetings.graph_get", new=AM(return_value=event_data)):

            async def fake_get(url, **kwargs):
                return _make_response(403, {"error": {"code": "Forbidden"}})

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = fake_get
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await meetings_get_transcript_by_event(
                    GetTranscriptByEventIdInput(event_id=SAMPLE_EVENT_ID),
                    CONTEXT,
                )

        assert "error" in result
        assert "organizer" in result["error"].lower() or "403" in result["error"]

    async def test_no_input_returns_error(self):
        """Calling with no IDs returns an immediate error without any HTTP calls."""
        from ms_graph_mcp.meetings import (
            GetTranscriptByEventIdInput,
            meetings_get_transcript_by_event,
        )

        result = await meetings_get_transcript_by_event(
            GetTranscriptByEventIdInput(),
            CONTEXT,
        )
        assert "error" in result
        assert "provide" in result["error"].lower() or "must" in result["error"].lower()

    async def test_no_transcript_returns_message(self):
        """A meeting with no transcripts returns a message, not an error."""
        from ms_graph_mcp.meetings import (
            GetTranscriptByEventIdInput,
            meetings_get_transcript_by_event,
        )

        async def fake_get(url, **kwargs):
            return _make_response(200, {"value": []})  # no transcripts

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = fake_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await meetings_get_transcript_by_event(
                GetTranscriptByEventIdInput(online_meeting_id=SAMPLE_ONLINE_MEETING_ID),
                CONTEXT,
            )

        assert "transcript" in result
        assert result["transcript"] == ""
        assert "message" in result


# ── 4. _parse_vtt ─────────────────────────────────────────────────────────────


class TestParseVtt:
    """Unit tests for the VTT → plain text helper."""

    def _parse(self, vtt: str) -> str:
        # Import the private helper via the module
        from ms_graph_mcp import meetings as graph_meetings

        return graph_meetings._parse_vtt(vtt)

    def test_strips_webvtt_header(self):
        vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nHello world."
        result = self._parse(vtt)
        assert "WEBVTT" not in result
        assert "Hello world." in result

    def test_strips_cue_numbers(self):
        vtt = "WEBVTT\n\n42\n00:00:01.000 --> 00:00:02.000\nSpeaker: Text here."
        result = self._parse(vtt)
        assert "42" not in result
        assert "Speaker: Text here." in result

    def test_strips_timestamps(self):
        vtt = "WEBVTT\n\n1\n00:01:23.456 --> 00:01:25.000\nSome spoken words."
        result = self._parse(vtt)
        assert "-->" not in result
        assert "Some spoken words." in result

    def test_multiple_cues_joined(self):
        vtt = (
            "WEBVTT\n\n"
            "1\n00:00:01.000 --> 00:00:02.000\nFirst line.\n\n"
            "2\n00:00:03.000 --> 00:00:04.000\nSecond line."
        )
        result = self._parse(vtt)
        assert "First line." in result
        assert "Second line." in result

    def test_empty_input_returns_empty(self):
        assert self._parse("") == ""
        assert self._parse(None) == ""  # type: ignore[arg-type]

    def test_speaker_names_preserved(self):
        vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:03.000\nAlice Smith: This is important."
        result = self._parse(vtt)
        assert "Alice Smith:" in result
        assert "This is important." in result
