"""Terminal, structured tool errors.

The dominant failure mode for a Graph MCP server is not a crash — it is a tool
that is visible to the caller but that this particular caller lacks the scope
for. Handed a bare HTTP 403, a model retries, gets 403 again, and loops. That
burns more tokens in production than any amount of schema does.

The fix is to tell the model, in the result, whether retrying can possibly help.
Models respect an explicit ``retryable: false`` far better than they infer intent
from a status code.

Every error carries:

  ``error``      a stable machine-readable code
  ``message``    what happened and what to do about it, phrased for a model
  ``retryable``  whether calling again with the same arguments could succeed

``server.py:_success_result`` marks any dict carrying an ``error`` key as
``isError: true``, which is what makes the client feed it back to the model
rather than treating it as a protocol fault. So these need no dispatch changes.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "GraphToolError",
    "conflict",
    "graph_error_response",
    "invalid_arguments",
    "not_found",
    "scope_denied",
    "throttled",
    "upstream_error",
]

# A tool error is just a dict — tools return it, they do not raise it. Raising
# would surface as a JSON-RPC protocol error, which clients are told NOT to feed
# back to the model, so the model would never learn why the call failed.
GraphToolError = dict[str, Any]


def _err(code: str, message: str, *, retryable: bool, **extra: Any) -> GraphToolError:
    return {"error": code, "message": message, "retryable": retryable, **extra}


def scope_denied(scope: str, *, tool: str = "") -> GraphToolError:
    """Graph refused because the caller's token lacks a delegated permission.

    Terminal by definition: the same token will be refused every time. The scope
    name is included so the model can tell the user precisely what to request
    rather than guessing.
    """
    what = f"Tool '{tool}' requires" if tool else "This operation requires"
    return _err(
        "SCOPE_DENIED",
        f"{what} the '{scope}' delegated permission, which the signed-in user's token does "
        f"not carry. Ask the user to request '{scope}' access. Do not retry this call.",
        retryable=False,
        scope=scope,
    )


def throttled(*, retry_after: float | None = None) -> GraphToolError:
    """Graph returned 429.

    Retryable, but only after waiting. Graph's limits are per-app-per-tenant, so
    one busy caller throttles everyone sharing the app registration — the wait is
    real and the model should not treat it as a transient blip.
    """
    wait = f" Wait {retry_after:g} seconds before retrying." if retry_after else " Back off first."
    return _err(
        "THROTTLED",
        f"Microsoft Graph is rate-limiting this application.{wait} "
        "Limits are shared across everything using this app registration.",
        retryable=True,
        retry_after_seconds=retry_after,
    )


def not_found(what: str) -> GraphToolError:
    """The addressed resource does not exist, or is not visible to this user.

    Not retryable with the same id. Graph deliberately does not distinguish
    "absent" from "no access", so the message must not claim it does.
    """
    return _err(
        "NOT_FOUND",
        f"{what} was not found, or the signed-in user cannot see it. Microsoft Graph does not "
        "distinguish between the two. Check the identifier before trying again.",
        retryable=False,
    )


def conflict(what: str, *, recovery: str = "") -> GraphToolError:
    """Optimistic-concurrency failure — 409 or 412.

    Retryable, but only after re-reading: the caller's version is stale, so
    repeating the identical request fails identically.
    """
    tail = f" {recovery}" if recovery else " Re-read the item and retry with the current version."
    return _err("CONFLICT", f"{what} changed since it was read.{tail}", retryable=True)


def invalid_arguments(message: str, **extra: Any) -> GraphToolError:
    """The arguments cannot work — a bad enum, a malformed id, a bad range.

    Retryable in the sense that corrected arguments will work, which is exactly
    what the model should do next.
    """
    return _err("INVALID_ARGUMENTS", message, retryable=True, **extra)


def upstream_error(status: int | None, detail: str = "") -> GraphToolError:
    """Anything else Graph returned that this layer has no better name for.

    5xx is worth retrying; a 4xx we did not classify is not.
    """
    retryable = bool(status and status >= 500)
    tail = f" {detail}" if detail else ""
    return _err(
        "UPSTREAM_ERROR",
        f"Microsoft Graph returned HTTP {status or 'error'}.{tail}"
        + (
            " This is usually transient."
            if retryable
            else " Do not retry without changing something."
        ),
        retryable=retryable,
        status_code=status,
    )


def graph_error_response(exc: Any, *, scope: str = "", tool: str = "") -> GraphToolError:
    """Translate an ``httpx.HTTPStatusError`` into the right terminal error.

    Centralised so the 403-and-429 mapping is written once rather than in every
    tool. Pass ``scope`` when the tool knows which permission it needs — that is
    what turns a generic 403 into an actionable instruction.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)

    if status == 403:
        return scope_denied(scope or "the required Graph permission", tool=tool)
    if status == 429:
        retry_after = None
        header = getattr(response, "headers", {}).get("Retry-After") if response else None
        if header:
            try:
                retry_after = float(header)
            except (TypeError, ValueError):
                retry_after = None
        return throttled(retry_after=retry_after)
    if status == 404:
        return not_found("The requested resource")
    if status in (409, 412):
        return conflict("The item")
    return upstream_error(status)
