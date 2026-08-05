# Debugging

Where to look when something misbehaves, in rough order of how often it is the answer.

## First: run the server in a terminal

MCP clients start the server as a subprocess and usually hide its stderr, so a startup failure
surfaces as nothing more than "server disconnected". Running the same command yourself is the single
highest-yield debugging step:

```bash
uv run --directory /path/to/ms-graph-mcp ms-graph-mcp
```

A healthy stdio server prints nothing and waits — it is speaking JSON-RPC on stdin/stdout. Anything
printed is either a diagnostic on stderr or a bug.

**stdout is the protocol channel on stdio.** A stray `print()` corrupts the JSON-RPC stream and the
client disconnects with a parse error that names nothing useful. All diagnostics — including the
interactive sign-in prompt and device code — go to **stderr** deliberately.

## Turning on logging

The default level is **WARNING**, so an ordinary run is quiet — the per-request lines below are
INFO, and a client that surfaces server output would otherwise show one line per Graph call to a
user who did not ask for it. Raise it to see them:

```bash
GRAPH_MCP_LOG_LEVEL=INFO uv run ms-graph-mcp-http     # per-request [Graph] lines
GRAPH_MCP_LOG_LEVEL=DEBUG uv run ms-graph-mcp         # plus SDK and transport internals
```

Everything goes to **stderr**, at every level, including on stdio where stdout is the protocol
channel. A misspelled level falls back to WARNING rather than refusing to start.

Every Graph call is logged by `client.py` with a `[Graph]` prefix, request and response on separate
lines:

```
[Graph] GET /me/messages
[Graph]   extra-headers=['ConsistencyLevel']
[Graph] GET /me/messages → 200
```

Read these for three things:

- **The path and query actually sent.** OData mistakes are usually visible here — a `$filter` that
  did not escape a quote, a `$select` naming a property that does not exist.
- **The `extra-headers` line.** If a call needs `ConsistencyLevel: eventual` and this line is
  missing, that is the bug. See the header gotcha below.
- **The status.** A `200` that returns nothing useful is a very different problem from a `403`.

Request bodies are not logged. They routinely contain message content, attendee lists and file
bytes, and a debug flag should not be the thing standing between a support ticket and someone's
mail.

## The error codes

Every tool failure is a structured value from `errors.py`, never an exception. A raised exception
becomes a JSON-RPC protocol error, which clients are told *not* to feed back to the model — so the
model would see a generic transport failure instead of an actionable message.

Each carries `retryable`, which is what stops a model looping on a 403.

| Code | Means | `retryable` | What should happen next |
|---|---|:--:|---|
| `SCOPE_DENIED` | Token lacks a delegated permission | `false` | Consent the named scope, sign in again |
| `THROTTLED` | Graph returned 429 | `true` | Wait `retry_after_seconds`, then retry |
| `NOT_FOUND` | Absent, or invisible to this user | `false` | Check the identifier |
| `CONFLICT` | 409/412 — stale etag | `true` | Re-read, then retry with the current version |
| `INVALID_ARGUMENTS` | Bad enum, malformed id, bad range | `true` | Fix the arguments and retry |
| `UPSTREAM_ERROR` | Anything else; 5xx is retryable, 4xx is not | varies | Retry only if `retryable` |
| `missing_graph_token` | No token reached dispatch | `false` | See below |
| `write_scope_required` | Write tool without write scope | `false` | Set `GRAPH_MCP_WRITE_SCOPE` / `X-Write-Scope` |
| `read_only_deployment` | `GRAPH_MCP_READ_ONLY` is on | `false` | Nothing the caller can do — this is the operator's setting |

`SCOPE_DENIED` names the exact permission in both `message` and a `scope` field, so the model can
tell the user what to request rather than guessing. If you add a tool, pass `scope=` to
`graph_error_response()` — that is what turns a generic 403 into something actionable.

**`Graph returned 200 but the fields are null`** is not an error code, it is the header gotcha.
`graph_get()` reserves `headers=` out of `**params`; passing headers any other way encodes them into
the query string, where Graph silently ignores them. Missing `ConsistencyLevel: eventual` on a
`memberOf` query is the classic case.

## Authentication failures

The largest category by far. Work out **which side** failed first — sign-in, or the call.

### Sign-in never completes (stdio)

| Symptom | Cause |
|---|---|
| `AADSTS7000218` | **Allow public client flows** is off on the app registration |
| `AADSTS50011` | Redirect URI is not `http://localhost`, or the platform is not *Public client/native* |
| Browser never opens | Expected over SSH or in a container — use the device code on stderr |
| Prompts on every call | `~/.ms-graph-mcp/token_cache.json` is not writable |
| `AADSTS53003` | Conditional Access wants a registered device — see the README |

`AADSTS53003` is the confusing one: your credentials were **accepted** and the sign-in succeeded; a
policy then refused to issue the token. Click **More details** and read `Device state`. If it says
`Unregistered`, no change to the app registration will fix it — the device needs registering, or an
admin needs to exclude the app id.

### The call is rejected (HTTP)

| Symptom | Cause |
|---|---|
| `401` with `Invalid audience` | The token is for Graph but the server expects its own audience, or the reverse. Check `GRAPH_MCP_DOES_OBO` |
| `401` with `azp` rejection | The token was minted by a different app registration. In the interim posture only OBO tokens from this `client_id` are accepted |
| `401`, `Missing Authorization header` | No token — or the request hit a path the middleware protects and you expected it public |
| `403` from a tool | Not authentication. The token is valid; the *permission* is missing — see `SCOPE_DENIED` |
| `421 Misdirected Request` | Not authentication either. The `Host` header is not trusted — set `GRAPH_MCP_RESOURCE_URL` |
| `AADSTS65001` during OBO | The user has not consented to the downstream Graph scopes |

The two postures validate different things, and mixing them up produces a confusing `401`:

- **Interim (default)** — the caller forwards an already-OBO'd Graph token. Validated for the Graph
  audience plus `azp == our client_id`.
- **Resource server** (`GRAPH_MCP_DOES_OBO=true`) — the inbound token is audienced to this MCP.
  Audience binding is the gate, so the `azp` check is dropped and the server exchanges the token
  itself.

### `missing_graph_token`

Dispatch fails closed before any Graph call. The message names the fix for the transport in use:
over stdio an env var (`GRAPH_MCP_CLIENT_ID` to sign in, or `GRAPH_MCP_ACCESS_TOKEN` to supply one
directly), over HTTP the `X-Graph-Token` header.

Seeing it on HTTP when a token *was* sent usually means the shared-secret machine bypass was taken:
a machine principal carries no Graph token by design, so any tool needing one fails closed here.
That is intended — it is why the internal tier gates on `is_machine` rather than `is_app_only`.

## A tool is missing from the list

Work down this list; it is almost always the first two.

1. **A toolset profile is hiding it.** The default is `core`, not `all` — 85 tool definitions is a
   lot to put in front of a model. Check `GRAPH_MCP_TOOLSETS`, and any `X-Toolsets` header.
   `GRAPH_MCP_TOOLSETS=all` shows everything.
2. **It is a write tool and the caller has no write scope**, or `GRAPH_MCP_READ_ONLY` is set.
3. **It is an internal tool.** Those never appear in `tools/list` — that is the point of the tier.
4. **It is in no allowlist.** A registered tool absent from every allowlist is unreachable. The
   reverse — an allowlist name with no registered tool — raises `RuntimeError` out of
   `resolve_read_tools()` rather than silently serving a partial surface.

Remember that hiding is not gating: a tool absent from `tools/list` can still be *called*, and the
tier gates are what refuse it. If a hidden tool executes, that is a security bug, not a filtering
bug.

## Tracing

Spans are emitted under the tracer `ms_graph_mcp`, one `graph.request` span per Graph call carrying
`http.status_code`.

OpenTelemetry here is **API-only**. Spans are no-ops unless the host process configures an SDK, so a
standalone run needs no exporter and costs nothing. To see them, configure an SDK in the process
that embeds this server.

## Reproducing what a client sees

When the server looks correct but a client misbehaves, drive it with a real client rather than
reasoning about it:

```bash
npx @modelcontextprotocol/inspector --cli uv run ms-graph-mcp --method tools/list
```

See [testing.md](testing.md) for the Inspector, including the environment-passthrough trap that
makes a config change look like it did nothing.

## Known gotchas

Collected in `CLAUDE.md`; the ones that cost the most time:

- **`client.py:_build_url` does not URL-encode `$` on purpose.** Switching to httpx `params=` breaks
  every OData query, and over-quoting double-encodes `%3a` inside JoinWebUrl filter values.
- **`graph_get_url()`'s host check is an SSRF guard.** It is the only helper taking a caller-supplied
  full URL; without the check a bearer-token request could be redirected off-host.
- **`mcp` 2.0 runs on `httpx2`, a distribution separate from `httpx`.** Both are installed — the SDK
  uses `httpx2`, `client.py` uses `httpx`. Do not mix them in one module.
- **`streamable_http_app()` owns the app's lifespan.** Replacing it means the transport never starts;
  chain onto `application.router.lifespan_context` instead.
