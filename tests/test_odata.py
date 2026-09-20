"""Tests for the OData sanitisation helpers."""

import pytest

from ms_graph_mcp.odata import (
    escape_odata_string,
    validate_graph_id,
    validate_mail_folder,
    validate_task_status,
)


class TestEscapeOdataString:
    def test_no_quotes(self):
        assert escape_odata_string("hello") == "hello"

    def test_single_quote(self):
        assert escape_odata_string("O'Brien") == "O''Brien"

    def test_injection_attempt(self):
        assert escape_odata_string("' or 1 eq 1 or '") == "'' or 1 eq 1 or ''"

    def test_empty(self):
        assert escape_odata_string("") == ""

    def test_multiple_quotes(self):
        assert escape_odata_string("it's a 'test'") == "it''s a ''test''"


class TestValidateGraphId:
    def test_valid_uuid(self):
        assert validate_graph_id("abc-123-DEF") == "abc-123-DEF"

    def test_valid_base64(self):
        assert validate_graph_id("AAMkAGE2OGUwY=") == "AAMkAGE2OGUwY="

    def test_valid_consumer_onenote_id(self):
        # Personal (consumer) Microsoft accounts put "!" in every OneDrive and
        # OneNote id. notes_list_sections returns these, so notes_list_pages has
        # to accept them -- otherwise OneNote is unreachable on such an account.
        assert validate_graph_id("0-AEFA61C7F4C45A8F!187") == "0-AEFA61C7F4C45A8F!187"
        assert validate_graph_id("AEFA61C7F4C45A8F!187") == "AEFA61C7F4C45A8F!187"

    def test_strips_surrounding_quotes(self):
        # Callers are frequently LLMs, and the rejection below used to render the
        # value with !r -- so the error displayed the id inside single quotes and
        # the next attempt copied them in, failing again for a different reason.
        assert validate_graph_id("'0-AEFA61C7F4C45A8F!187'") == "0-AEFA61C7F4C45A8F!187"
        assert validate_graph_id('"0-AEFA61C7F4C45A8F!187"') == "0-AEFA61C7F4C45A8F!187"
        assert validate_graph_id("  0-AEFA61C7F4C45A8F!187  ") == "0-AEFA61C7F4C45A8F!187"

    def test_quotes_are_stripped_not_permitted(self):
        # A quote anywhere but the ends is still a rejection, and stripping is
        # not a way round the traversal check.
        with pytest.raises(ValueError):
            validate_graph_id("abc'def")
        with pytest.raises(ValueError, match="Invalid Graph API ID"):
            validate_graph_id("'../../etc/passwd'")

    def test_error_does_not_quote_the_value(self):
        # The message must not show the id wrapped in quotes: that is what a
        # caller copies back.
        with pytest.raises(ValueError) as excinfo:
            validate_graph_id("bad id")
        assert "'bad id'" not in str(excinfo.value)
        assert "unquoted" in str(excinfo.value)

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="Invalid Graph API ID"):
            validate_graph_id("../../etc/passwd")

    def test_rejects_spaces(self):
        with pytest.raises(ValueError):
            validate_graph_id("some id")

    def test_rejects_odata_injection(self):
        with pytest.raises(ValueError):
            validate_graph_id("') or 1 eq 1 --")

    def test_rejects_traversal_even_with_bang(self):
        # "!" is allowed, but it does not become a way round the ".." check.
        with pytest.raises(ValueError, match="Invalid Graph API ID"):
            validate_graph_id("a!../../etc/passwd")


class TestValidateMailFolder:
    @pytest.mark.parametrize(
        "folder", ["inbox", "sentitems", "drafts", "all", "deleteditems", "junkemail"]
    )
    def test_valid_folders(self, folder):
        assert validate_mail_folder(folder) == folder

    def test_case_insensitive(self):
        assert validate_mail_folder("INBOX") == "inbox"

    def test_rejects_unknown(self):
        with pytest.raises(ValueError, match="Unknown mail folder"):
            validate_mail_folder("../../etc/passwd")

    def test_rejects_injection(self):
        with pytest.raises(ValueError):
            validate_mail_folder("inbox' or '1'='1")


class TestValidateTaskStatus:
    @pytest.mark.parametrize("status", ["notStarted", "inProgress", "completed", "all"])
    def test_valid_statuses(self, status):
        assert validate_task_status(status) == status

    def test_rejects_unknown(self):
        with pytest.raises(ValueError, match="Unknown task status"):
            validate_task_status("' or 1 eq 1")
