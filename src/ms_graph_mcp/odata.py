"""OData sanitisation helpers for Microsoft Graph API queries."""

from __future__ import annotations

import re

# Graph API object IDs are alphanumeric with hyphens, underscores, dots,
# equals, plus, and forward-slash (base64 segments).
_GRAPH_ID_RE = re.compile(r"^[a-zA-Z0-9\-_\.=+/]+$")

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

    Raises ``ValueError`` if the value contains characters that could be
    used for path traversal or injection.
    """
    if not _GRAPH_ID_RE.match(value) or ".." in value:
        raise ValueError(f"Invalid Graph API ID for {param_name}: {value!r}")
    return value


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
