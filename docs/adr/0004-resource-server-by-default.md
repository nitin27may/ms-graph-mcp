# ADR 0004 — The HTTP transport is an OAuth resource server by default

- **Status:** Accepted
- **Date:** 2026-08-05
- **Supersedes:** nothing. Complements [ADR 0003](0003-no-gateway-trust-mode.md).

## Context

The HTTP transport has always had two postures, selected by `GRAPH_MCP_DOES_OBO`:

- **Passthrough** (`false`, the previous default) — the caller forwards a token it has already
  exchanged for Microsoft Graph. The server validates `aud == https://graph.microsoft.com` and
  narrows it with `azp == our client_id`.
- **Resource server** (`true`) — the caller presents a token audienced to *this* server, and the
  server performs the on-behalf-of exchange itself before calling Graph.

Passthrough was the default because this server was extracted from a platform where the calling
agent already held a Graph token. Outside that platform, it is the wrong default.

**The MCP authorization specification requires a server to validate that a token was issued for
it**, and to reject tokens that were not — even correctly signed, unexpired ones from a trusted
issuer. Token passthrough is named as an anti-pattern. A Graph-audienced token fails that test: it
was issued for Graph. `azp` establishes who *minted* a token, not who it is *for*, so it narrows the
set of acceptable tokens without making any of them audience-bound to this server.

Microsoft's own on-behalf-of documentation gives the matching warning about relaying middle-tier
tokens, and lists the consequences. One of them decides this:

> Inability to satisfy token binding and Conditional Access scenarios requiring claim step-up (for
> example, MFA, Sign-in Frequency).

That is not hypothetical here. The tenant this server was built against enforces Conditional Access
— `AADSTS53003` is documented in the troubleshooting guide as a failure users hit.

## Decision

`GRAPH_MCP_DOES_OBO` defaults to `true`. The HTTP transport is a resource server unless explicitly
configured otherwise.

Passthrough remains available, deprecated, with removal scheduled for 1.0
(`src/ms_graph_mcp/deprecations.py`). It warns at startup.

**`GRAPH_MCP_DOES_OBO` is an HTTP-transport setting with no meaning for stdio.**

## Consequences

### The stdio transport had to stop sharing the exchange

This is the part that made the change non-trivial. The exchange used to live in
`dispatch_graph_tool`, which **both** transports share, gated only on `mcp_does_obo`:

```python
# server.py, before
cfg = get_config()
if cfg.mcp_does_obo:
    graph_token = await acquire_token_on_behalf_of(context["access_token"], …)
```

A stdio token comes from interactive sign-in and is *already* a Graph token. Entra refuses to redeem
a token audienced to another app — "Applications can't redeem a token for a different app" — so
flipping the default with the exchange in that position would have broken **every local client** —
VS Code, Claude Code, Claude Desktop — on its first tool call. Verified: with the old code and the
setting on, a stdio call reached out to `login.microsoftonline.com` and failed.

stdio was safe because of a default, not because of a boundary.

The exchange therefore moved into `auth.py`, the HTTP middleware, which stdio never runs. The
coupling is now structural: there is no code path by which a stdio session can perform an OBO
exchange, misconfigured or not. `tests/test_stdio_unaffected.py` asserts that from the outside, with
the setting turned on.

### Claims challenges became possible

Moving the exchange to the middleware fixed a functional defect rather than merely relocating code.
A failed exchange used to become a structured error inside a **200** `CallToolResult`, so a
Conditional Access claims challenge never reached the client and step-up could not complete.

Dispatch cannot fix this — it has no access to the HTTP response. The middleware can, and now
answers `401` with the challenge in `WWW-Authenticate`, which is what Microsoft's guidance
prescribes and what a client can act on.

A configuration fault stays a `500`: a `401` there would loop the client against a problem no token
can solve.

### The exchange runs once per session rather than per call

The middleware runs for every authenticated request, including `initialize` and `tools/list`, which
do not need Graph. MSAL caches on (assertion, scopes), so this is one network round-trip per session
rather than per tool call — and surfacing an auth failure at session start rather than at the first
tool call is better behaviour, not worse.

The alternative — peeking at the JSON-RPC method in the request body to decide whether to exchange —
costs body-replay plumbing for no real gain.

### Existing passthrough deployments must act

A deployment running the previous default and no client secret will now refuse to start, naming both
the credential options and the `GRAPH_MCP_DOES_OBO=false` opt-out. This is a real break, called out
in the changelog. Failing at startup is the point: the alternative is failing on an arbitrary tool
call later.

### Audience validation no longer depends on signature verification

Found while implementing this: the unverified decode path (`GRAPH_MCP_JWT_VERIFY=false`) checked
expiry and issuer but **not audience**. With signature checking off — documented as a narrow escape
hatch for a local run with no JWKS connectivity — the audience gate silently disappeared, and a
Graph-audienced token was accepted.

Turning off signature verification narrows one check. It must not abandon the audience gate too:
validating `aud` needs no signing key, and it is the requirement this ADR is about. Fixed in
`entra/jwt_verify.py`.

## Alternatives considered

**Keep the default and document it.** Rejected. The safe value should be the one you get by doing
nothing; this is the same argument that made `GRAPH_MCP_JWT_VERIFY` default to `true` in 0.2.0.
Documentation does not prevent a class of mistake that arises from not reading it.

**Remove passthrough entirely.** Rejected for now. The originating platform runs it, and migrating
requires an app registration change — exposing a scope, re-consenting — that cannot be done in a
deploy. Deprecated with a removal version instead, which the deprecation register enforces.

## References

- MCP authorization specification — resource servers, RFC 9728, RFC 8707, the token-passthrough
  prohibition
- [OAuth 2.0 On-Behalf-Of flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow)
  — the middle-tier warning and the claims-challenge pattern
- [Agent OAuth flows: on-behalf-of](https://learn.microsoft.com/en-us/entra/agent-id/agent-on-behalf-of-oauth-flow)
  — Entra Agent ID, and the guidance against secrets in production
- `docs/agent-auth.md` — the setup this decision implies
- `tests/test_stdio_unaffected.py` — the regression guard for the transport split
