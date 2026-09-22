"""OData sanitisation helpers for Microsoft Graph API queries."""

from __future__ import annotations

import re

# Graph API object IDs are alphanumeric with hyphens, underscores, dots,
# equals, plus, forward-slash (base64 segments), and "!".
#
# "!" is what every consumer (personal Microsoft account) OneDrive and OneNote
# id contains -- "0-AEFA61C7F4C45A8F!187" for a OneNote section, "AEFA61C7F4C45A8F!187"
# for the drive item behind it. Without it, notes_list_sections happily returns
# those ids and notes_list_pages then refuses every one of them, so OneNote is
# unreachable on a personal account. "!" is not a path separator and ".." is
# rejected separately below, so accepting it does not widen the traversal surface.
_GRAPH_ID_RE = re.compile(r"^[a-zA-Z0-9\-_\.=+/!]+$")

# Allowed mail-folder slugs for the /mailFolders/{slug} path segment.
_VALID_FOLDERS = frozenset({"inbox", "sentitems", "drafts", "deleteditems", "junkemail", "all"})

# Allowed To-Do task statuses for OData $filter.
_VALID_TASK_STATUSES = frozenset({"notStarted", "inProgress", "completed", "all"})


def escape_odata_string(value: str) -> str:
    """Escape a value for use inside OData single-quoted string literals.

    OData uses doubled single-quotes as the escape sequence for a literal
    single-quote inside a string, e.g.  ``name eq 'O''Brien'``.
    """
    return value.replace("'", "''")


def validate_graph_id(value: str, param_name: str = "id") -> str:
    """Validate that *value* looks like a Microsoft Graph object ID.

    Surrounding whitespace and one matching pair of quotes are stripped first.
    Callers are frequently LLMs, and an id arrives quoted often enough to be
    worth accepting -- all the more so because the rejection below used to
    render the value with ``!r``, so the error itself displayed the id inside
    single quotes and the next attempt copied them in. Stripping ends that
    loop; the quotes are removed, not permitted, so the character class stays
    as strict as it was.

    Raises ``ValueError`` if the value contains characters that could be
    used for path traversal or injection.
    """
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "'\"":
        cleaned = cleaned[1:-1].strip()
    if not _GRAPH_ID_RE.match(cleaned) or ".." in cleaned:
        # Deliberately NOT ``!r``: quoting the value here is what taught a
        # caller to send it quoted. Say what is allowed instead.
        raise ValueError(
            f"Invalid Graph API ID for {param_name}: {cleaned} "
            "(allowed: letters, digits and - _ . = + / !, sent unquoted)"
        )
    return cleaned


def validate_mail_folder(value: str) -> str:
    """Return *value* if it is a recognised mail-folder slug, else raise."""
    lower = value.lower()
    if lower not in _VALID_FOLDERS:
        raise ValueError(f"Unknown mail folder: {value!r}")
    return lower


def validate_task_status(value: str) -> str:
    """Return *value* if it is a valid To-Do task status, else raise."""
    if value not in _VALID_TASK_STATUSES:
        raise ValueError(f"Unknown task status: {value!r}")
    return value
