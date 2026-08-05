"""Graph Email tools — search, recent, flagged, and thread retrieval."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from ms_graph_mcp.client import graph_get, graph_post_no_content
from ms_graph_mcp.config import get_config
from ms_graph_mcp.odata import escape_odata_string, validate_graph_id, validate_mail_folder
from ms_graph_mcp.tooling import READ_ONLY, WRITE_CREATE, WRITE_SEND, tool

_SELECT = "id,subject,from,toRecipients,receivedDateTime,bodyPreview,importance,flag,conversationId,webLink"
_SELECT_FULL = _SELECT + ",body"


class SearchEmailsInput(BaseModel):
    query: str = Field(description="Search keywords, subject, or sender name")
    max_results: int = Field(10, description="Maximum emails to return (1–50)")
    folder: str = Field("inbox", description="Mail folder: inbox | sentitems | drafts | all")


class GetRecentEmailsInput(BaseModel):
    days_back: int = Field(7, description="Number of days back to fetch")
    max_results: int = Field(20, description="Maximum emails to return")


class GetFlaggedEmailsInput(BaseModel):
    max_results: int = Field(15, description="Maximum flagged emails to return")


class GetEmailThreadInput(BaseModel):
    conversation_id: str = Field(
        description="The email conversationId to fetch all messages in the thread"
    )
    max_results: int = Field(20, description="Maximum messages in the thread to return")


@tool(
    description=(
        "Search the signed-in user's mailbox by keyword, subject or sender name. Returns id, "
        "subject, sender, date and a preview snippet, newest first. Searchable folders are "
        "inbox, sentitems, drafts, or all. Use mail_list_recent when the ask is time-based "
        "rather than keyword-based, and mail_get_thread to read a whole conversation. "
        "Requires Mail.Read."
    ),
    annotations=READ_ONLY,
    aliases=("search_emails",),
)
async def mail_search(params: SearchEmailsInput, context: dict) -> list[dict]:
    token = context["access_token"]
    folder = validate_mail_folder(params.folder)
    folder_path = "" if folder == "all" else f"/mailFolders/{folder}"
    data = await graph_get(
        token,
        f"/me{folder_path}/messages",
        **{
            "$search": f'"{params.query}"',
            "$select": _SELECT,
            "$top": min(params.max_results, 50),
        },
    )
    return [_slim_msg(m) for m in (data.get("value") or [])]


@tool(
    description=(
        "List the signed-in user's most recent emails from the last N days, newest first. "
        "Returns id, subject, sender, date and a preview snippet. Use for 'what came in today' "
        "or catching up after time away; mail_search is the tool when specific keywords or a "
        "sender are known. Requires Mail.Read."
    ),
    annotations=READ_ONLY,
    aliases=("get_recent_emails",),
)
async def mail_list_recent(params: GetRecentEmailsInput, context: dict) -> list[dict]:
    token = context["access_token"]
    since = (datetime.now(UTC) - timedelta(days=params.days_back)).isoformat()
    data = await graph_get(
        token,
        "/me/messages",
        **{
            "$filter": f"receivedDateTime ge {since}",
            "$select": _SELECT,
            "$top": min(params.max_results, 50),
            "$orderby": "receivedDateTime desc",
        },
    )
    return [_slim_msg(m) for m in (data.get("value") or [])]


@tool(
    description=(
        "List emails the signed-in user has flagged for follow-up, newest first. Returns id, "
        "subject, sender, date and preview. Flags are the user's own marker for 'come back to "
        "this', so use it for 'what do I still need to deal with' rather than mail_list_recent, "
        "which returns everything regardless of state. Requires Mail.Read."
    ),
    annotations=READ_ONLY,
    aliases=("get_flagged_emails",),
)
async def mail_list_flagged(params: GetFlaggedEmailsInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me/messages",
        **{
            "$filter": "flag/flagStatus eq 'flagged'",
            "$select": _SELECT,
            "$top": min(params.max_results, 50),
            "$orderby": "receivedDateTime desc",
        },
    )
    return [_slim_msg(m) for m in (data.get("value") or [])]


@tool(
    description=(
        "Read every message in one email conversation, oldest first, given a conversation id "
        "from any of the mail listing tools. Returns full message bodies rather than previews, "
        "so it is the tool for understanding what was actually discussed before replying or "
        "summarising. Requires Mail.Read."
    ),
    annotations=READ_ONLY,
    aliases=("get_email_thread",),
)
async def mail_get_thread(params: GetEmailThreadInput, context: dict) -> list[dict]:
    token = context["access_token"]
    data = await graph_get(
        token,
        "/me/messages",
        **{
            "$filter": f"conversationId eq '{escape_odata_string(params.conversation_id)}'",
            "$select": _SELECT_FULL,
            "$top": min(params.max_results, 50),
            "$orderby": "receivedDateTime asc",
        },
    )
    return [_full_msg(m) for m in (data.get("value") or [])]


class SendEmailInput(BaseModel):
    to_recipients: list[str] = Field(description="List of recipient email addresses")
    cc_recipients: list[str] = Field(default_factory=list, description="List of CC email addresses")
    subject: str = Field(description="Email subject line")
    body_html: str = Field(description="Email body as HTML")


# ── H1 Step B — propose_email + ConfirmEmailCard flow ───────────────────────


async def _event_attendee_emails(
    token: str, event_id: str, *, exclude_email: str = ""
) -> list[str]:
    """Return the lowercased, deduped recipient list for a calendar event.

    Reads ``/me/events/{event_id}`` and pulls the organizer + every
    attendee's ``emailAddress.address``. ``exclude_email`` filters out
    the caller so they aren't on their own outgoing email by default.

    Used by both ``propose_email`` (for display in the confirm card)
    AND the REST send endpoint (for the authoritative recipient list).
    The endpoint re-derives at send time so a browser-supplied
    recipient list cannot drift from the calendar truth — that's the
    H1 Step B exfiltration-safety property.
    """
    try:
        event = await graph_get(
            token,
            f"/me/events/{event_id}",
            params={"$select": "attendees,organizer"},
        )
    except Exception:
        return []
    recipients: list[str] = []
    organizer = (event or {}).get("organizer") or {}
    org_addr = (organizer.get("emailAddress") or {}).get("address")
    if org_addr:
        recipients.append(org_addr)
    for att in (event or {}).get("attendees") or []:
        addr = (att.get("emailAddress") or {}).get("address")
        if addr:
            recipients.append(addr)
    excl = (exclude_email or "").strip().lower()
    seen: set[str] = set()
    out: list[str] = []
    for addr in recipients:
        norm = (addr or "").strip().lower()
        if not norm or norm == excl or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _is_external(addr: str, user_email: str) -> bool:
    """True when ``addr``'s domain differs from ``user_email``'s.

    Used to flag cross-tenant recipients on the confirm card so the
    user sees a red highlight before clicking Send.
    """
    a = (addr or "").strip().lower()
    u = (user_email or "").strip().lower()
    if "@" not in a or "@" not in u:
        return False
    return a.rsplit("@", 1)[1] != u.rsplit("@", 1)[1]


class ProposeEmailInput(BaseModel):
    """Input for the ``propose_email`` tool.

    Crucially has NO ``recipients`` field — the LLM cannot pick who
    receives the email. Recipients are derived server-side from
    ``event_id`` at both display time (for the confirm card) and again
    at send time (re-derived; browser list ignored). This is the
    primary safety property: an injection that tells the LLM to
    "send this to attacker@evil.com" cannot succeed because the LLM
    has no way to influence the recipient list.
    """

    event_id: str = Field(
        description="Calendar event id (e.g. from get_meeting_details). "
        "Recipients are derived server-side from this event's attendee list."
    )
    subject: str = Field(description="Email subject line the user will see before sending.")
    body_html: str = Field(description="Email body as HTML the user will see before sending.")


@tool(
    description=(
        "Draft an email about a meeting for the user to review and send themselves — it does "
        "NOT send anything. Returns a confirmation card. Recipients are derived server-side "
        "from the calendar event's attendee list and cannot be set by the caller, which is what "
        "makes this safe to use on untrusted input. Requires Calendars.Read."
    ),
    annotations=WRITE_CREATE,
    aliases=("propose_email",),
)
async def mail_propose(params: ProposeEmailInput, context: dict) -> dict:
    token = context.get("access_token", "")
    caller = (context.get("user_email") or "").strip().lower()
    if not token:
        return {"error": "missing_access_token", "message": "Graph token required"}
    recipients = await _event_attendee_emails(token, params.event_id, exclude_email=caller)
    if not recipients:
        return {
            "error": "no_recipients",
            "message": (
                f"Event {params.event_id} has no attendees besides you, or could "
                "not be fetched. Cannot propose an email with no recipients."
            ),
        }
    external = [a for a in recipients if _is_external(a, caller)]
    return {
        "type": "confirm_email",
        "title": "Confirm email",
        "event_id": params.event_id,
        "subject": params.subject,
        "body_html": params.body_html,
        "recipients": recipients,
        "external_recipients": external,
        "caller_email": caller,
    }


def _check_send_email_allowed_domains(
    to_recipients: list[str], cc_recipients: list[str]
) -> str | None:
    """Reject the send when ``SEND_EMAIL_ALLOWED_DOMAINS`` is set and any
    recipient sits outside the allowlist.

    Returns an error string when the send must be refused, or ``None`` to
    allow it. Empty / unset env = no enforcement (backward compatible).
    """
    raw = (get_config().send_email_allowed_domains or "").strip()
    if not raw:
        return None
    allowed = {d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()}
    if not allowed:
        return None
    rejected: list[str] = []
    for addr in [*to_recipients, *cc_recipients]:
        addr_lower = (addr or "").strip().lower()
        if "@" not in addr_lower:
            rejected.append(addr)
            continue
        domain = addr_lower.rsplit("@", 1)[1]
        if domain not in allowed:
            rejected.append(addr)
    if rejected:
        return (
            f"Recipients outside SEND_EMAIL_ALLOWED_DOMAINS={sorted(allowed)}: {rejected}. "
            "send_email refused at the tool layer; ask the user to confirm the recipient or "
            "update the deployment's allowlist."
        )
    return None


@tool(
    description=(
        "Send an email as the signed-in user, with an HTML body and optional CC recipients. The "
        "message is sent immediately with no confirmation step, so confirm recipients and "
        "content with the user first. A deployment may restrict recipient domains, in which "
        "case the send is refused before it reaches Graph. Requires Mail.Send."
    ),
    annotations=WRITE_SEND,
    aliases=("send_email",),
)
async def mail_send(params: SendEmailInput, context: dict) -> dict:
    token = context["access_token"]

    # Tenant-domain allowlist (security-todo low/nits) — opt-in. When
    # unset the tool keeps its previous behaviour; when set, recipients
    # outside the allowlist are rejected before any Graph call so a
    # hijacked agent cannot exfiltrate via cross-tenant addresses.
    allow_error = _check_send_email_allowed_domains(params.to_recipients, params.cc_recipients)
    if allow_error is not None:
        return {"error": "recipient_not_allowed", "message": allow_error}

    mail_body: dict = {
        "message": {
            "subject": params.subject,
            "body": {"contentType": "HTML", "content": params.body_html},
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in params.to_recipients],
        },
        "saveToSentItems": True,
    }
    if params.cc_recipients:
        mail_body["message"]["ccRecipients"] = [
            {"emailAddress": {"address": addr}} for addr in params.cc_recipients
        ]

    # sendMail answers 202 Accepted with an empty body, so graph_post's
    # resp.json() cannot be used — graph_post_no_content exists for exactly this
    # shape and keeps the tracing span and [Graph] error logging.
    await graph_post_no_content(token, "/me/sendMail", mail_body)

    return {
        "status": "sent",
        "subject": params.subject,
        "to": params.to_recipients,
        "cc": params.cc_recipients,
    }


# ── Track F (Wave 2b) — attachment fetch (non-tool helper) ──────────────────


# Subset of MIME types we know how to extract text from. Anything else
# (images that aren't ID-able, videos, archives) is skipped — fetching
# would burn bandwidth without giving the LLM something to embed.
_ATTACHMENT_MIME_ALLOW = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain",
    "text/markdown",
    "text/csv",
}

# Some senders hand us extension-only metadata; map those to MIME so the
# allow-list still catches them.
_EXT_TO_MIME = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
}


async def fetch_message_attachments(
    access_token: str,
    message_id: str,
    *,
    max_mb: int = 25,
) -> list[dict]:
    """Track F (Wave 2b) — list + download non-inline file attachments.

    Returns a list of ``{id, name, contentType, size, content_bytes}``
    dicts, each ≤ ``max_mb`` MB and matching ``_ATTACHMENT_MIME_ALLOW``
    or a known office extension. ``isInline`` attachments are filtered
    out — those are typically signature-block logos / image-in-body
    decorations that have no retrieval value.

    Failures (Graph errors, oversized files, unsupported MIME types)
    are swallowed and logged so a single bad attachment doesn't block
    the rest of the email's ingest. Returns ``[]`` on any list-call
    failure so the caller's spawn loop is a no-op.
    """
    import base64

    max_bytes = max(1, max_mb) * 1024 * 1024

    # Step 1 — list metadata. ``$select`` keeps the response small;
    # ``$filter`` drops inline attachments at the server (no point
    # paginating through them just to reject locally).
    try:
        meta = await graph_get(
            access_token,
            f"/me/messages/{message_id}/attachments",
            **{
                "$select": "id,name,contentType,size,isInline,@odata.type",
                "$filter": "isInline eq false",
                "$top": 50,
            },
        )
    except Exception as exc:
        # Don't downgrade to debug — a Graph error here can mean the
        # token lost Mail.Read after a re-consent flow, which the
        # operator needs to see.
        import logging

        logging.getLogger(__name__).warning(
            "[email.attachments] list failed for %s: %s",
            message_id,
            exc,
        )
        return []

    out: list[dict] = []
    for item in meta.get("value") or []:
        if item.get("isInline"):
            continue
        # Microsoft.Graph.referenceAttachment / itemAttachment carry no
        # binary content — skip; only fileAttachment has contentBytes.
        odata_type = (item.get("@odata.type") or "").lower()
        if "fileattachment" not in odata_type:
            continue
        size = int(item.get("size") or 0)
        if size > max_bytes:
            continue
        name = (item.get("name") or "").strip()
        mime = (item.get("contentType") or "").strip().lower()
        if mime not in _ATTACHMENT_MIME_ALLOW:
            ext_mime = _EXT_TO_MIME.get(_ext_of(name))
            if ext_mime is None:
                continue
            mime = ext_mime

        # Step 2 — fetch the binary. Graph returns ``contentBytes`` as
        # base64 inline; we decode here so callers receive raw bytes
        # ready for hashing + extraction.
        try:
            full = await graph_get(
                access_token,
                f"/me/messages/{message_id}/attachments/{item['id']}",
                # No $select — we need contentBytes which Graph excludes
                # from the list-shape projection.
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "[email.attachments] fetch failed for %s: %s",
                item.get("id"),
                exc,
            )
            continue
        encoded = full.get("contentBytes")
        if not encoded:
            continue
        try:
            content_bytes = base64.b64decode(encoded)
        except (ValueError, TypeError) as exc:
            import logging

            logging.getLogger(__name__).warning(
                "[email.attachments] base64 decode failed for %s: %s",
                item.get("id"),
                exc,
            )
            continue
        # Defence in depth — server lying about size shouldn't blow
        # downstream extractors.
        if len(content_bytes) > max_bytes:
            continue

        out.append(
            {
                "id": item["id"],
                "name": name or "attachment",
                "contentType": mime,
                "size": len(content_bytes),
                "content_bytes": content_bytes,
            }
        )

    return out


def _ext_of(name: str) -> str:
    """Extract the lowercased extension (with dot) from a filename, or
    empty string when the name has none."""
    if not name or "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1].lower()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _slim_msg(m: dict) -> dict:
    sender = (m.get("from") or {}).get("emailAddress", {})
    return {
        "id": m.get("id", ""),
        "subject": m.get("subject", ""),
        "from_email": sender.get("address", ""),
        "from_name": sender.get("name", ""),
        "date": m.get("receivedDateTime", ""),
        "snippet": (m.get("bodyPreview") or "")[:250],
        "importance": m.get("importance", "normal"),
        "flagged": (m.get("flag") or {}).get("flagStatus") == "flagged",
        "conversation_id": m.get("conversationId", ""),
        "web_link": m.get("webLink", ""),
    }


def _full_msg(m: dict) -> dict:
    base = _slim_msg(m)
    body = m.get("body") or {}
    base["body_text"] = body.get("content", "")[:3000]  # cap at 3k chars
    return base


class ListEmailAttachmentsInput(BaseModel):
    message_id: str = Field(description="The message id to list attachments for")


@tool(
    description=(
        "List the file attachments on one email — name, content type and size in bytes. Returns "
        "metadata only and never file contents, so it is safe to call on messages with very "
        "large attachments. Inline images such as signature logos are excluded. Requires "
        "Mail.Read."
    ),
    annotations=READ_ONLY,
    aliases=("list_email_attachments",),
)
async def mail_list_attachments(params: ListEmailAttachmentsInput, context: dict) -> list[dict]:
    """Attachment metadata for the agent surface.

    Deliberately not the internal ``fetch_message_attachments``, which downloads
    every attachment and base64-encodes it. Handing an LLM a base64 blob is
    both useless to it and enormously expensive in tokens — a model asking
    "what's attached?" wants filenames. Downloading stays in the internal tier
    for the ETL callers that actually parse the bytes.
    """
    token = context["access_token"]
    message_id = validate_graph_id(params.message_id, "message_id")
    data = await graph_get(
        token,
        f"/me/messages/{message_id}/attachments",
        **{"$select": "id,name,contentType,size,isInline"},
    )
    return [
        {
            "id": a.get("id", ""),
            "name": a.get("name", ""),
            "content_type": a.get("contentType", ""),
            "size_bytes": a.get("size", 0),
        }
        for a in (data.get("value") or [])
        if not a.get("isInline")
    ]
