"""
Shared httpx helpers for Microsoft Graph REST API.

All tools receive the user's delegated access token via context["access_token"].
TLS verification is disabled for corporate proxy compatibility (mirrors the
TypeScript Graph client in web/src/lib/graph/client.ts).
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from dataclasses import dataclass

import httpx
from opentelemetry import trace

from ms_graph_mcp.config import get_config

logger = logging.getLogger(__name__)
_tracer = trace.get_tracer("ms_graph_mcp")

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TIMEOUT = httpx.Timeout(30.0)


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _log_error(resp: httpx.Response) -> None:
    """Log Graph API errors with the full response body for diagnostics."""
    try:
        body = resp.json()
        err = body.get("error", {})
        code = err.get("code", "unknown")
        msg = err.get("message", resp.text)
        logger.error(
            "[Graph] %s %s → %s %s: %s",
            resp.request.method,
            resp.url,
            resp.status_code,
            code,
            msg,
            extra={
                "graph.status_code": resp.status_code,
                "graph.error_code": code,
                "graph.url": str(resp.url),
                "graph.method": resp.request.method,
            },
        )
        # Record as a span event so it appears in Aspire trace detail
        # even when the structured-logs view is empty.
        span = trace.get_current_span()
        span.add_event(
            "graph.error",
            attributes={
                "graph.status_code": resp.status_code,
                "graph.error_code": code,
                "graph.error_message": msg[:500],
                "graph.url": str(resp.url),
                "graph.method": resp.request.method,
            },
        )
    except Exception:
        logger.error(
            "[Graph] %s %s → %s: %s",
            resp.request.method,
            resp.url,
            resp.status_code,
            resp.text[:500],
        )


def _build_url(base: str, **params) -> str:
    """
    Build a URL with OData query params ($filter, $select, etc.) as literal keys.

    Two httpx 0.28 problems:
      1. params={} encodes $ → %24  (breaks $filter, $select, …)
      2. urllib.parse.quote(safe=',') double-encodes %3a → %253a inside
         JoinWebUrl filter values.  The Graph OData parser expects the filter
         value VERBATIM — it does NOT url-decode it a second time.

    Fix: use a very permissive safe set that only encodes chars that genuinely
    break query-string parsing (space → %20, + → %2B, # → %23).
    Everything else — %, /, :, ', ?, =, commas, brackets — stays literal,
    matching how the MS Graph JS SDK passes OData parameters.
    """
    if not params:
        return base
    # Keep everything literal except space, +, and #
    _SAFE = "/:@!$&'()*,;=?%[]{}\"~"
    parts = [f"{k}={urllib.parse.quote(str(v), safe=_SAFE)}" for k, v in params.items()]
    return f"{base}?{'&'.join(parts)}"


async def graph_get(access_token: str, path: str, **params) -> dict:
    """GET request to Microsoft Graph. Returns the parsed JSON body.

    Extra HTTP headers (e.g. ``ConsistencyLevel: eventual`` for advanced
    queries using ``$filter`` / ``$search`` / ``$orderby`` / OData casts)
    can be supplied via the reserved ``headers=`` kwarg. Everything else in
    ``**params`` is treated as an OData query parameter. This split matters
    because OData keys start with ``$`` — there's no collision with
    ``headers`` as a plain identifier — but we have to extract it BEFORE
    passing the rest to ``_build_url``.

    Without this split, callers that tried to pass ``headers={...}`` (see
    the legacy ``integrations/graph/directory.py`` search path) silently
    had their header dict encoded into the URL as ``?headers=%7B...%7D``
    and Graph ignored it. That's the root cause of the "memberOf returns
    200 but every typed property is null" symptom on the Entra UI page:
    the cast query ``/memberOf/microsoft.graph.group`` is classified by
    Graph as an "advanced query" and requires the header — when missing,
    Graph returns the rows but strips typed-only fields to null.
    """
    extra_headers = params.pop("headers", None) or {}
    url = _build_url(f"{_GRAPH_BASE}{path}", **params)
    request_headers = _headers(access_token)
    for k, v in extra_headers.items():
        request_headers[k] = str(v)
    with _tracer.start_as_current_span(
        "graph.request",
        attributes={"http.method": "GET", "http.url": url},
    ) as span:
        async with httpx.AsyncClient(
            verify=not get_config().disable_ssl_verify, timeout=_TIMEOUT
        ) as client:
            logger.info("[Graph] GET %s", path)
            if extra_headers:
                logger.info("[Graph]   extra-headers=%s", list(extra_headers.keys()))
            resp = await client.get(url, headers=request_headers)
            span.set_attribute("http.status_code", resp.status_code)
            if not resp.is_success:
                span.set_status(trace.StatusCode.ERROR, f"HTTP {resp.status_code}")
                _log_error(resp)
            else:
                logger.info("[Graph] GET %s → %s", path, resp.status_code)
            resp.raise_for_status()
            return resp.json()


async def graph_get_text(access_token: str, path: str, **params) -> str:
    """GET request to Microsoft Graph returning raw response text.

    Use when the endpoint returns non-JSON content (e.g. transcript VTT,
    iCal, CSV).  Behaviour is identical to graph_get except the response
    body is returned as a string instead of being JSON-parsed.
    """
    extra_headers = params.pop("headers", None) or {}
    url = _build_url(f"{_GRAPH_BASE}{path}", **params)
    request_headers = _headers(access_token)
    # Override Accept so Graph doesn't force a JSON wrapper around the
    # binary/text content (VTT, iCal, …).
    request_headers["Accept"] = "*/*"
    for k, v in extra_headers.items():
        request_headers[k] = str(v)
    with _tracer.start_as_current_span(
        "graph.request",
        attributes={"http.method": "GET", "http.url": url},
    ) as span:
        async with httpx.AsyncClient(
            verify=not get_config().disable_ssl_verify, timeout=_TIMEOUT
        ) as client:
            logger.info("[Graph] GET %s (text)", path)
            resp = await client.get(url, headers=request_headers)
            span.set_attribute("http.status_code", resp.status_code)
            if not resp.is_success:
                span.set_status(trace.StatusCode.ERROR, f"HTTP {resp.status_code}")
                _log_error(resp)
            else:
                logger.info("[Graph] GET %s → %s", path, resp.status_code)
            resp.raise_for_status()
            return resp.text


async def graph_post(access_token: str, path: str, body: dict) -> dict:
    """POST request to Microsoft Graph. Returns the parsed JSON body."""
    url = f"{_GRAPH_BASE}{path}"
    with _tracer.start_as_current_span(
        "graph.request",
        attributes={"http.method": "POST", "http.url": url},
    ) as span:
        async with httpx.AsyncClient(
            verify=not get_config().disable_ssl_verify, timeout=_TIMEOUT
        ) as client:
            logger.info("[Graph] POST %s", path)
            resp = await client.post(url, headers=_headers(access_token), json=body)
            span.set_attribute("http.status_code", resp.status_code)
            if not resp.is_success:
                span.set_status(trace.StatusCode.ERROR, f"HTTP {resp.status_code}")
                _log_error(resp)
            else:
                logger.info("[Graph] POST %s → %s", path, resp.status_code)
            resp.raise_for_status()
            return resp.json()


async def graph_post_no_content(
    access_token: str,
    path: str,
    body: dict | None = None,
    extra_headers: dict[str, str] | None = None,
) -> None:
    """POST to Microsoft Graph for an action endpoint that returns no body.

    Graph's *action* endpoints — ``sendMail``, ``reply``, ``replyAll``,
    ``forward``, ``accept``, ``decline``, ``cancel`` — answer ``202 Accepted``
    with an empty body. :func:`graph_post` ends in ``resp.json()`` and therefore
    blows up on exactly those calls, which is why several tools historically
    hand-rolled ``httpx`` and lost the tracing span, the ``[Graph]`` error log,
    and the TLS toggle in the process.

    Returns ``None`` on success and raises ``httpx.HTTPStatusError`` otherwise.
    Tolerates a body if one is sent — some endpoints return ``201`` with content
    that the caller does not need.
    """
    url = f"{_GRAPH_BASE}{path}"
    headers = _headers(access_token)
    if extra_headers:
        headers.update(extra_headers)
    with _tracer.start_as_current_span(
        "graph.request",
        attributes={"http.method": "POST", "http.url": url},
    ) as span:
        async with httpx.AsyncClient(
            verify=not get_config().disable_ssl_verify, timeout=_TIMEOUT
        ) as client:
            logger.info("[Graph] POST %s", path)
            resp = await client.post(url, headers=headers, json=body if body is not None else {})
            span.set_attribute("http.status_code", resp.status_code)
            if not resp.is_success:
                span.set_status(trace.StatusCode.ERROR, f"HTTP {resp.status_code}")
                _log_error(resp)
            else:
                logger.info("[Graph] POST %s → %s", path, resp.status_code)
            resp.raise_for_status()


@dataclass(frozen=True)
class GraphResult:
    """A Graph response whose status the caller wants to branch on.

    ``status_code`` is the whole point: several Graph flows treat a specific
    failure as a signal rather than an error. A 403 from the ``JoinWebUrl``
    filter means "you attended this meeting but did not organise it", which
    selects a different lookup strategy — not something to raise on.
    """

    status_code: int
    ok: bool
    text: str
    content_type: str = ""
    _payload: dict | None = None

    def json(self) -> dict:
        """Parsed body, or ``{}`` when the response was not JSON."""
        return self._payload or {}


async def graph_try_get(
    access_token: str,
    path: str,
    *,
    accept: str = "application/json",
    timeout_seconds: float = 30.0,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = False,
    **params,
) -> GraphResult:
    """GET from Microsoft Graph and return the outcome **without raising**.

    :func:`graph_get` calls ``raise_for_status()``, which is right when any
    failure is an error. It is wrong wherever the status itself carries meaning
    — an attendee hitting an organizer-only filter, or a transcript that simply
    has no content yet.

    Before this existed those callers hand-rolled ``httpx``, and in doing so
    lost the tracing span, the ``[Graph]`` error logging and the TLS toggle. Use
    this instead of reaching for a client.
    """
    extra = dict(headers or {})
    url = _build_url(f"{_GRAPH_BASE}{path}", **params) if params else f"{_GRAPH_BASE}{path}"
    request_headers = _headers(access_token)
    request_headers["Accept"] = accept
    request_headers.update(extra)

    with _tracer.start_as_current_span(
        "graph.request",
        attributes={"http.method": "GET", "http.url": url},
    ) as span:
        async with httpx.AsyncClient(
            verify=not get_config().disable_ssl_verify,
            timeout=httpx.Timeout(timeout_seconds),
            # File content endpoints answer 302 with a short-lived download URL
            # on a different host, so the redirect has to be followed to get any
            # bytes at all.
            follow_redirects=follow_redirects,
        ) as client:
            logger.info("[Graph] GET %s", path)
            resp = await client.get(url, headers=request_headers)
            span.set_attribute("http.status_code", resp.status_code)
            if not resp.is_success:
                # Recorded as an event, not a span error: the caller may well
                # treat this status as an expected branch.
                span.add_event("graph.non_success", {"status_code": resp.status_code})
                _log_error(resp)
            else:
                logger.info("[Graph] GET %s → %s", path, resp.status_code)

            payload: dict | None = None
            if resp.is_success and accept.startswith("application/json"):
                try:
                    payload = resp.json()
                except ValueError:
                    payload = None
            return GraphResult(
                status_code=resp.status_code,
                ok=resp.is_success,
                text=resp.text,
                content_type=resp.headers.get("content-type", ""),
                _payload=payload,
            )


async def graph_post_raw(
    access_token: str,
    path: str,
    content: bytes,
    content_type: str,
    *,
    timeout_seconds: float = 30.0,
) -> dict:
    """POST a non-JSON body to Microsoft Graph and return the parsed JSON reply.

    A few Graph endpoints take a raw entity body rather than a JSON document —
    OneNote page creation wants ``text/html``, for instance. :func:`graph_post`
    serialises its argument as JSON and so cannot express those, which is why
    they used to hand-roll ``httpx`` and lose the tracing span, the ``[Graph]``
    error logging and the TLS toggle.
    """
    url = f"{_GRAPH_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": content_type,
        "Accept": "application/json",
    }
    with _tracer.start_as_current_span(
        "graph.request",
        attributes={"http.method": "POST", "http.url": url},
    ) as span:
        async with httpx.AsyncClient(
            verify=not get_config().disable_ssl_verify,
            timeout=httpx.Timeout(timeout_seconds),
        ) as client:
            logger.info("[Graph] POST %s (%s)", path, content_type)
            resp = await client.post(url, headers=headers, content=content)
            span.set_attribute("http.status_code", resp.status_code)
            if not resp.is_success:
                span.set_status(trace.StatusCode.ERROR, f"HTTP {resp.status_code}")
                _log_error(resp)
            else:
                logger.info("[Graph] POST %s → %s", path, resp.status_code)
            resp.raise_for_status()
            return resp.json()


async def graph_put_raw(
    access_token: str,
    path: str,
    content: bytes,
    content_type: str,
    *,
    extra_headers: dict[str, str] | None = None,
    timeout_seconds: float = 60.0,
) -> GraphResult:
    """PUT a raw byte body to a Graph path, returning the outcome without raising.

    File uploads and in-place content replacement both PUT bytes rather than a
    JSON document, and both need to branch on the status: 412 means the item
    changed under an ``If-Match``, which the caller reports as a conflict rather
    than a failure.
    """
    url = f"{_GRAPH_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": content_type or "application/octet-stream",
    }
    if extra_headers:
        headers.update(extra_headers)
    with _tracer.start_as_current_span(
        "graph.request",
        attributes={"http.method": "PUT", "http.url": url, "graph.upload.size": len(content)},
    ) as span:
        async with httpx.AsyncClient(
            verify=not get_config().disable_ssl_verify,
            timeout=httpx.Timeout(timeout_seconds),
        ) as client:
            logger.info("[Graph] PUT %s (%d bytes)", path, len(content))
            resp = await client.put(url, headers=headers, content=content)
            span.set_attribute("http.status_code", resp.status_code)
            if not resp.is_success:
                span.add_event("graph.non_success", {"status_code": resp.status_code})
                _log_error(resp)
            else:
                logger.info("[Graph] PUT %s → %s", path, resp.status_code)
            payload: dict | None = None
            if resp.is_success:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = None
            return GraphResult(
                status_code=resp.status_code,
                ok=resp.is_success,
                text=resp.text,
                content_type=resp.headers.get("content-type", ""),
                _payload=payload,
            )


async def graph_patch(
    access_token: str,
    path: str,
    body: dict,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    """
    PATCH request to Microsoft Graph.

    Some PATCH endpoints (Planner, OneNote) require an ``If-Match`` header
    with an ETag obtained from a preceding GET. Pass it via ``extra_headers``.
    """
    url = f"{_GRAPH_BASE}{path}"
    headers = _headers(access_token)
    if extra_headers:
        headers.update(extra_headers)
    with _tracer.start_as_current_span(
        "graph.request",
        attributes={"http.method": "PATCH", "http.url": url},
    ) as span:
        async with httpx.AsyncClient(
            verify=not get_config().disable_ssl_verify, timeout=_TIMEOUT
        ) as client:
            logger.info("[Graph] PATCH %s", path)
            resp = await client.patch(url, headers=headers, json=body)
            span.set_attribute("http.status_code", resp.status_code)
            if not resp.is_success:
                span.set_status(trace.StatusCode.ERROR, f"HTTP {resp.status_code}")
                _log_error(resp)
            else:
                logger.info("[Graph] PATCH %s → %s", path, resp.status_code)
            resp.raise_for_status()
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()


async def graph_delete(
    access_token: str,
    path: str,
    extra_headers: dict[str, str] | None = None,
) -> None:
    """
    DELETE request to Microsoft Graph. Returns None on success.

    Like PATCH, some Planner/OneNote deletes require an ``If-Match`` header
    with an ETag from a preceding GET.
    """
    url = f"{_GRAPH_BASE}{path}"
    headers = _headers(access_token)
    if extra_headers:
        headers.update(extra_headers)
    with _tracer.start_as_current_span(
        "graph.request",
        attributes={"http.method": "DELETE", "http.url": url},
    ) as span:
        async with httpx.AsyncClient(
            verify=not get_config().disable_ssl_verify, timeout=_TIMEOUT
        ) as client:
            logger.info("[Graph] DELETE %s", path)
            resp = await client.delete(url, headers=headers)
            span.set_attribute("http.status_code", resp.status_code)
            if not resp.is_success:
                span.set_status(trace.StatusCode.ERROR, f"HTTP {resp.status_code}")
                _log_error(resp)
            else:
                logger.info("[Graph] DELETE %s → %s", path, resp.status_code)
            resp.raise_for_status()


async def graph_probe_status(
    access_token: str, path: str, *, timeout_seconds: float = 10.0
) -> int | None:
    """GET a Graph ``path`` and return the HTTP status code WITHOUT raising.

    For access-revalidation probes that must distinguish 200 / 403 / 404 / 429 /
    5xx rather than treat every non-200 as an error (so they can't use
    ``graph_get``, which raises). Returns ``None`` on transport failure. The one
    place Graph probe GETs live, so the revalidation sweep doesn't hand-roll httpx.
    """
    url = f"{_GRAPH_BASE}{path}"
    try:
        async with httpx.AsyncClient(
            verify=not get_config().disable_ssl_verify,
            timeout=httpx.Timeout(timeout_seconds),
        ) as client:
            resp = await client.get(url, headers=_headers(access_token))
            return resp.status_code
    except httpx.HTTPError:
        return None


async def graph_get_url(
    access_token: str,
    url: str,
    *,
    extra_headers: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> dict:
    """GET a fully-built Graph URL (e.g. an ``@odata.nextLink`` cursor) → JSON.

    Pagination bakes the page cursor into the URL, so it can't be rebuilt from
    path + params — this is the single home for nextLink fetches. Retries once on
    429, honouring ``Retry-After`` (capped at 30s).

    Host-allowlisted to Graph (Phase 0 item 7, agentic audit) — every other
    helper in this file is pinned to ``_GRAPH_BASE`` by construction; this is
    the one that takes a full caller-supplied URL, so without a check here a
    bearer-token-carrying request could be redirected to an arbitrary host
    (SSRF / delegated-token exfiltration). A real ``@odata.nextLink`` is
    always a ``graph.microsoft.com`` URL — Microsoft mints it, we don't.
    """
    if not url.startswith(f"{_GRAPH_BASE}/") and url != _GRAPH_BASE:
        raise ValueError(f"graph_get_url: URL must be under {_GRAPH_BASE}, got {url!r}")
    headers = {**_headers(access_token), "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    async with httpx.AsyncClient(
        verify=not get_config().disable_ssl_verify, timeout=httpx.Timeout(timeout_seconds)
    ) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                sleep_for = min(float(retry_after), 30.0) if retry_after else 1.0
            except ValueError:
                sleep_for = 1.0
            await asyncio.sleep(sleep_for)
            resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()
