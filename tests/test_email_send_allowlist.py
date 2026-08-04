"""``send_email`` tenant-domain allowlist.

Covers the low/nit follow-up in ``security-todo.md``: high-risk
deployments can set ``SEND_EMAIL_ALLOWED_DOMAINS`` (comma-separated
list) to force the ``send_email`` tool to refuse recipients outside the
tenant + explicit partner domains.  Unset / empty preserves the prior
unrestricted behaviour.

The gate reads ``get_config().send_email_allowed_domains`` from the
``ms_graph_mcp`` package config (env ``SEND_EMAIL_ALLOWED_DOMAINS`` /
``GRAPH_MCP_SEND_EMAIL_ALLOWED_DOMAINS``); tests override the cached
singleton's attribute.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from ms_graph_mcp.config import get_config
from ms_graph_mcp.email import (
    SendEmailInput,
    _check_send_email_allowed_domains,
    send_email,
)


def _allowlist(value: str):
    """Patch the package config's send-email allowlist for the test scope."""
    return patch.object(get_config(), "send_email_allowed_domains", value)


class TestCheckAllowedDomains:
    """The pure helper drives the gate — no Graph calls involved."""

    def test_empty_setting_allows_anything(self):
        # Default config has SEND_EMAIL_ALLOWED_DOMAINS = "" — gate disabled.
        with _allowlist(""):
            assert _check_send_email_allowed_domains(["a@anywhere.com"], []) is None

    def test_recipient_in_allowlist_passes(self):
        with _allowlist("contoso.com,partners.com"):
            assert _check_send_email_allowed_domains(["bob@contoso.com"], []) is None

    def test_partner_domain_in_cc_passes(self):
        with _allowlist("contoso.com,partners.com"):
            assert (
                _check_send_email_allowed_domains(["bob@contoso.com"], ["x@partners.com"]) is None
            )

    def test_external_recipient_rejected(self):
        with _allowlist("contoso.com"):
            error = _check_send_email_allowed_domains(["bob@contoso.com", "attacker@evil.com"], [])
        assert error is not None
        assert "attacker@evil.com" in error

    def test_case_insensitive_domain_match(self):
        with _allowlist("ConTosO.com"):
            assert _check_send_email_allowed_domains(["BOB@contoso.com"], []) is None

    def test_address_without_at_sign_rejected(self):
        with _allowlist("contoso.com"):
            error = _check_send_email_allowed_domains(["not-an-email"], [])
        assert error is not None
        assert "not-an-email" in error


class TestSendEmailTool:
    """The tool returns a structured error and SKIPS the Graph network
    call when the allowlist gate fires."""

    def test_returns_structured_error_when_recipient_outside_allowlist(self):
        with (
            _allowlist("contoso.com"),
            patch("httpx.AsyncClient") as mock_client,
        ):
            params = SendEmailInput(
                to_recipients=["attacker@evil.com"],
                subject="exfil",
                body_html="<p>data</p>",
            )
            result = asyncio.run(send_email(params, {"access_token": "tok"}))

        assert result["error"] == "recipient_not_allowed"
        assert "attacker@evil.com" in result["message"]
        # Critical: httpx must not have been constructed — the rejection
        # short-circuits before any Graph call.
        mock_client.assert_not_called()

    def test_allows_send_when_recipient_in_allowlist(self):
        """When every recipient is on the allowlist, the tool proceeds to
        the Graph call (we mock httpx to assert it was actually invoked)."""

        class _FakeResp:
            status_code = 202

            def raise_for_status(self):
                return None

        captured_call: dict = {}

        class _FakeClient:
            def __init__(self, **_):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, url, **kwargs):
                captured_call["url"] = url
                captured_call["json"] = kwargs.get("json")
                return _FakeResp()

        with (
            _allowlist("contoso.com"),
            patch("httpx.AsyncClient", _FakeClient),
        ):
            params = SendEmailInput(
                to_recipients=["bob@contoso.com"],
                subject="Status update",
                body_html="<p>weekly notes</p>",
            )
            result = asyncio.run(send_email(params, {"access_token": "tok"}))

        assert result["status"] == "sent"
        assert captured_call["url"].endswith("/me/sendMail")
        assert captured_call["json"]["message"]["subject"] == "Status update"
