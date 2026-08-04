"""H1 Step B — propose_email tool contract.

The tool returns a ``confirm_email`` card payload; it MUST NOT call
sendMail.  Recipients are derived server-side from the event_id —
the LLM has no input field to influence them, so even an injection
that asks "send this to attacker@evil.com" cannot succeed because
the tool's input schema doesn't accept a recipients argument.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ms_graph_mcp.email import (
    ProposeEmailInput,
    _is_external,
    propose_email,
)


def _ctx(*, user_email: str = "alice@contoso.com", token: str = "graph-tok") -> dict:
    return {"access_token": token, "user_email": user_email}


def _event_payload(*, attendees: list[str], organizer: str | None = None) -> dict:
    """Build the shape /me/events/{id} returns when $select=attendees,organizer."""
    return {
        "attendees": [{"emailAddress": {"address": a}} for a in attendees],
        "organizer": ({"emailAddress": {"address": organizer}} if organizer else {}),
    }


# ── happy paths ─────────────────────────────────────────────────────────


async def test_returns_confirm_email_card():
    with patch(
        "ms_graph_mcp.email.graph_get",
        AsyncMock(
            return_value=_event_payload(
                attendees=["bob@contoso.com", "carol@contoso.com"],
                organizer="alice@contoso.com",
            )
        ),
    ):
        result = await propose_email(
            ProposeEmailInput(event_id="evt-1", subject="Recap", body_html="<p>hi</p>"),
            _ctx(),
        )
    assert result["type"] == "confirm_email"
    assert result["subject"] == "Recap"
    assert result["body_html"] == "<p>hi</p>"
    assert result["event_id"] == "evt-1"
    assert result["caller_email"] == "alice@contoso.com"


async def test_recipients_derived_from_event_attendees():
    """The tool ignores any LLM intent — recipients ONLY come from the
    Graph event's attendee list. There is no input parameter that
    could carry an attacker-chosen recipient."""
    with patch(
        "ms_graph_mcp.email.graph_get",
        AsyncMock(
            return_value=_event_payload(
                attendees=["bob@contoso.com", "carol@contoso.com"],
                organizer="alice@contoso.com",
            )
        ),
    ):
        result = await propose_email(
            ProposeEmailInput(event_id="evt-1", subject="r", body_html=""),
            _ctx(),
        )
    # alice is the caller — excluded. bob + carol remain.
    assert set(result["recipients"]) == {"bob@contoso.com", "carol@contoso.com"}


async def test_excludes_caller_from_recipients():
    """The caller is excluded so they aren't on their own outgoing email."""
    with patch(
        "ms_graph_mcp.email.graph_get",
        AsyncMock(
            return_value=_event_payload(
                attendees=["alice@contoso.com", "bob@contoso.com"],
                organizer="alice@contoso.com",
            )
        ),
    ):
        result = await propose_email(
            ProposeEmailInput(event_id="evt-1", subject="r", body_html=""),
            _ctx(user_email="alice@contoso.com"),
        )
    assert "alice@contoso.com" not in result["recipients"]
    assert "bob@contoso.com" in result["recipients"]


async def test_dedupes_organizer_when_also_an_attendee():
    """Microsoft Graph often returns the organizer in BOTH the organizer
    block AND the attendees list. The dedup pass keeps a single entry."""
    with patch(
        "ms_graph_mcp.email.graph_get",
        AsyncMock(
            return_value=_event_payload(
                attendees=["bob@contoso.com", "bob@contoso.com"],
                organizer="bob@contoso.com",
            )
        ),
    ):
        result = await propose_email(
            ProposeEmailInput(event_id="evt-1", subject="r", body_html=""),
            _ctx(),
        )
    assert result["recipients"].count("bob@contoso.com") == 1


async def test_flags_external_recipients():
    """external_recipients is the subset whose domain differs from the
    caller's. Frontend highlights these in red on the confirm card."""
    with patch(
        "ms_graph_mcp.email.graph_get",
        AsyncMock(
            return_value=_event_payload(
                attendees=[
                    "bob@contoso.com",  # internal
                    "carol@partners.com",  # external
                    "dave@external.co",  # external
                ],
            )
        ),
    ):
        result = await propose_email(
            ProposeEmailInput(event_id="evt-1", subject="r", body_html=""),
            _ctx(user_email="alice@contoso.com"),
        )
    assert "bob@contoso.com" not in result["external_recipients"]
    assert "carol@partners.com" in result["external_recipients"]
    assert "dave@external.co" in result["external_recipients"]


# ── safety / fail-closed paths ──────────────────────────────────────────


async def test_no_send_mail_call():
    """propose_email never calls /me/sendMail — it only reads /me/events."""
    sendmail_mock = AsyncMock()
    with (
        patch(
            "ms_graph_mcp.email.graph_get",
            AsyncMock(return_value=_event_payload(attendees=["bob@contoso.com"])),
        ),
        # No httpx send-mail patch needed; if propose_email called
        # /me/sendMail at all the test would import httpx & fail
        # network. The structural guarantee here is the function
        # body's lack of any send call — assertion is the absence of
        # a sendMail invocation via the existing sendmail_mock side
        # channel; mark it explicitly:
        patch("ms_graph_mcp.email.send_email", sendmail_mock),
    ):
        await propose_email(
            ProposeEmailInput(event_id="evt-1", subject="r", body_html=""),
            _ctx(),
        )
    assert sendmail_mock.await_count == 0


async def test_returns_no_recipients_error_when_event_empty():
    """An event with no attendees (or that fails to fetch) yields an
    error rather than a card. The frontend never sees an empty
    confirm dialog."""
    with patch(
        "ms_graph_mcp.email.graph_get",
        AsyncMock(return_value=_event_payload(attendees=[])),
    ):
        result = await propose_email(
            ProposeEmailInput(event_id="evt-1", subject="r", body_html=""),
            _ctx(),
        )
    assert "type" not in result or result.get("type") != "confirm_email"
    assert result["error"] == "no_recipients"


async def test_returns_no_recipients_error_when_only_caller_attends():
    """An event where the only attendee IS the caller still yields the
    no_recipients error — there's nobody else to email."""
    with patch(
        "ms_graph_mcp.email.graph_get",
        AsyncMock(
            return_value=_event_payload(
                attendees=["alice@contoso.com"],
                organizer="alice@contoso.com",
            )
        ),
    ):
        result = await propose_email(
            ProposeEmailInput(event_id="evt-1", subject="r", body_html=""),
            _ctx(user_email="alice@contoso.com"),
        )
    assert result["error"] == "no_recipients"


async def test_returns_token_error_when_context_missing_token():
    """Defensive — no Graph token in context means we can't read the
    event; refuse to fabricate recipients."""
    result = await propose_email(
        ProposeEmailInput(event_id="evt-1", subject="r", body_html=""),
        {"user_email": "alice@contoso.com"},  # no access_token
    )
    assert result["error"] == "missing_access_token"


async def test_input_schema_has_no_recipients_field():
    """Structural guarantee — the LLM cannot supply a recipient list
    because the input model doesn't accept one. The model_json_schema
    is what OpenAI sees as the tool's input schema."""
    schema = ProposeEmailInput.model_json_schema()
    fields = schema.get("properties", {})
    assert "recipients" not in fields
    assert "to_recipients" not in fields
    assert "cc_recipients" not in fields
    # The three fields the LLM CAN set:
    assert "event_id" in fields
    assert "subject" in fields
    assert "body_html" in fields


# ── _is_external helper ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "addr,user,expected",
    [
        ("bob@contoso.com", "alice@contoso.com", False),  # same domain
        ("bob@partners.com", "alice@contoso.com", True),  # external
        ("BoB@Contoso.com", "alice@CONTOSO.com", False),  # case-insensitive
        ("", "alice@contoso.com", False),  # malformed
        ("bob@contoso.com", "", False),  # malformed
    ],
)
def test_is_external(addr, user, expected):
    assert _is_external(addr, user) is expected
