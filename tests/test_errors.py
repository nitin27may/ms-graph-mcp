"""Terminal structured errors.

The property under test throughout is `retryable`. A model handed a bare 403
retries, gets 403 again, and loops — which costs more in production than schema
tokens ever do. These tests pin down which failures say "try again" and which
say "stop", because getting that backwards is worse than having no signal.
"""

from __future__ import annotations

import httpx
import pytest

from ms_graph_mcp.errors import (
    conflict,
    graph_error_response,
    invalid_arguments,
    not_found,
    scope_denied,
    throttled,
    upstream_error,
)


def _http_error(status: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://graph.microsoft.com/v1.0/me")
    response = httpx.Response(status, request=request, headers=headers or {})
    return httpx.HTTPStatusError("boom", request=request, response=response)


class TestRetryability:
    """The single field that decides whether the agent loops."""

    def test_scope_denied_is_terminal(self):
        err = scope_denied("Mail.Read")
        assert err["retryable"] is False
        assert err["scope"] == "Mail.Read"
        # The scope must appear in the prose too — that is what the model relays
        # to the user when asking for access.
        assert "Mail.Read" in err["message"]
        assert "not retry" in err["message"].lower()

    def test_throttled_is_retryable_and_carries_the_wait(self):
        err = throttled(retry_after=30)
        assert err["retryable"] is True
        assert err["retry_after_seconds"] == 30
        assert "30" in err["message"]

    def test_not_found_is_terminal(self):
        err = not_found("The event")
        assert err["retryable"] is False

    def test_not_found_does_not_claim_the_item_is_absent(self):
        """Graph returns 404 for both 'gone' and 'no access'. Saying which is a lie."""
        message = not_found("The event")["message"].lower()
        assert "cannot see it" in message or "not distinguish" in message

    def test_conflict_is_retryable_but_only_after_re_reading(self):
        err = conflict("The task")
        assert err["retryable"] is True
        assert "re-read" in err["message"].lower()

    def test_invalid_arguments_is_retryable_because_corrected_args_work(self):
        assert invalid_arguments("bad enum")["retryable"] is True

    def test_server_errors_are_retryable_client_errors_are_not(self):
        assert upstream_error(503)["retryable"] is True
        assert upstream_error(400)["retryable"] is False
        assert upstream_error(None)["retryable"] is False


class TestGraphErrorMapping:
    """One place translates HTTP status to meaning, so 90 tools need not."""

    def test_403_becomes_scope_denied_with_the_named_scope(self):
        err = graph_error_response(
            _http_error(403), scope="Calendars.ReadWrite", tool="create_event"
        )
        assert err["error"] == "SCOPE_DENIED"
        assert err["scope"] == "Calendars.ReadWrite"
        assert "create_event" in err["message"]
        assert err["retryable"] is False

    def test_403_without_a_known_scope_still_terminates(self):
        err = graph_error_response(_http_error(403))
        assert err["error"] == "SCOPE_DENIED"
        assert err["retryable"] is False

    def test_429_becomes_throttled_and_honours_retry_after(self):
        err = graph_error_response(_http_error(429, {"Retry-After": "12"}))
        assert err["error"] == "THROTTLED"
        assert err["retry_after_seconds"] == 12
        assert err["retryable"] is True

    def test_429_with_an_unparseable_retry_after_still_works(self):
        """Graph may send an HTTP-date instead of seconds; it must not crash."""
        err = graph_error_response(
            _http_error(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
        )
        assert err["error"] == "THROTTLED"
        assert err["retry_after_seconds"] is None
        assert err["retryable"] is True

    def test_429_with_no_header_is_still_throttled(self):
        err = graph_error_response(_http_error(429))
        assert err["error"] == "THROTTLED"
        assert err["retryable"] is True

    @pytest.mark.parametrize("status", [409, 412])
    def test_concurrency_failures_map_to_conflict(self, status):
        """Planner and OneDrive both use these for stale-etag writes."""
        assert graph_error_response(_http_error(status))["error"] == "CONFLICT"

    def test_404_maps_to_not_found(self):
        assert graph_error_response(_http_error(404))["error"] == "NOT_FOUND"

    def test_unclassified_status_falls_through_to_upstream_error(self):
        err = graph_error_response(_http_error(418))
        assert err["error"] == "UPSTREAM_ERROR"
        assert err["status_code"] == 418

    def test_an_exception_with_no_response_does_not_crash(self):
        err = graph_error_response(RuntimeError("no response attribute"))
        assert err["error"] == "UPSTREAM_ERROR"
        assert err["retryable"] is False


class TestShape:
    def test_every_error_carries_the_three_required_keys(self):
        for err in (
            scope_denied("X.Y"),
            throttled(),
            not_found("thing"),
            conflict("thing"),
            invalid_arguments("nope"),
            upstream_error(500),
        ):
            assert {"error", "message", "retryable"} <= err.keys()
            assert isinstance(err["retryable"], bool)
            assert err["error"].isupper(), "codes are stable machine-readable constants"

    def test_errors_are_dicts_not_exceptions(self):
        """Raising would surface as a JSON-RPC protocol error.

        Clients are told not to feed those back to the model, so the model would
        never learn why the call failed — which is the entire point.
        """
        assert isinstance(scope_denied("X.Y"), dict)
