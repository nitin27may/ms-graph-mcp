"""``mail_send`` tenant-domain allowlist.

Covers the low/nit follow-up in ``security-todo.md``: high-risk
deployments can set ``SEND_EMAIL_ALLOWED_DOMAINS`` (comma-separated
list) to force the ``mail_send`` tool to refuse recipients outside the
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
    mail_send,
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
            patch("ms_graph_mcp.email.graph_post_no_content") as mock_post,
        ):
            params = SendEmailInput(
                to_recipients=["attacker@evil.com"],
                subject="exfil",
                body_html="<p>data</p>",
            )
            result = asyncio.run(mail_send(params, {"access_token": "tok"}))

        assert result["error"] == "recipient_not_allowed"
        assert "attacker@evil.com" in result["message"]
        # Critical: the Graph call must not have been reached — the rejection
        # short-circuits before it.
        mock_post.assert_not_called()

    def test_allows_send_when_recipient_in_allowlist(self):
        """When every recipient is on the allowlist the tool proceeds to Graph.

        Mocked at the client seam rather than at httpx: mail_send goes through
        ``graph_post_no_content`` because /me/sendMail answers 202 with an empty
        body, and patching httpx here would only re-test the client module.
        """
        captured: dict = {}

        async def _fake_post(token, path, body=None, extra_headers=None):
            captured["token"] = token
            captured["path"] = path
            captured["body"] = body

        with (
            _allowlist("contoso.com"),
            patch("ms_graph_mcp.email.graph_post_no_content", _fake_post),
        ):
            params = SendEmailInput(
                to_recipients=["bob@contoso.com"],
                subject="Status update",
                body_html="<p>weekly notes</p>",
            )
            result = asyncio.run(mail_send(params, {"access_token": "tok"}))

        assert result["status"] == "sent"
        assert captured["path"] == "/me/sendMail"
        assert captured["token"] == "tok"
        assert captured["body"]["message"]["subject"] == "Status update"
