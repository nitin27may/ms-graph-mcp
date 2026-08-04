# ADR 0002 — Call Microsoft Graph with raw `httpx`, not `msgraph-sdk`

- **Status:** Accepted
- **Date:** 2026-08-04 (documenting a decision made during the original build)

## Context

The obvious way to call Microsoft Graph from Python is `msgraph-sdk` with `azure-identity` for
credentials. This project does neither: `src/ms_graph_mcp/client.py` issues Graph calls with
`httpx` directly, and tokens arrive from the caller through the request context rather than from a
credential chain.

This ADR records why, because the alternative looks strictly better until you look closely.

## Decision

Keep the Graph client as raw `httpx`. `msgraph-sdk` and `azure-identity` remain deliberately absent
from `dependencies` in `pyproject.toml`.

## Consequences

**In favour:**

- **Dependency weight.** `msgraph-sdk` pulls in the Kiota runtime stack and a very large generated
  model package. For a server whose job is to hand JSON to a language model, none of that typing is
  used — the models get serialised straight back to JSON.
- **Wire behaviour stays inspectable.** Several endpoints this server depends on need control the
  SDK abstracts away: `ConsistencyLevel: eventual` on advanced directory queries, `Accept: */*` for
  VTT transcript bodies, and OData query strings that must not be re-encoded (see `_build_url`).
  When Graph misbehaves, the failing request is three lines away.
- **Auth is the caller's, not the library's.** Tokens are supplied per request — a delegated token
  forwarded by an agent, an OBO exchange result, or a client-credentials token for the app-only
  internal tier. `azure-identity`'s credential-chain model assumes the process owns the identity,
  which is the opposite of this server's posture.
- **TLS control.** Corporate-proxy environments need `verify=False` as an explicit, auditable
  config toggle (`GRAPH_MCP_DISABLE_SSL_VERIFY`).

**Against, accepted knowingly:**

- Every endpoint is hand-written. New Graph surface means new code, not a regenerated client.
- No compile-time typing of Graph responses. Mitigated by keeping each tool's output shaped by an
  explicit slimming helper (`_slim_event`, `_full_event`, …) rather than passing raw Graph JSON on.
- Retry, throttling and pagination are ours to implement. Currently: 429 retry with `Retry-After` in
  `graph_get_url`, and explicit `@odata.nextLink` walking where needed.

**Constraint this creates:** the MCP Python SDK 2.0 depends on `httpx2`, a distribution separate
from `httpx`. Migrating the SDK therefore puts two HTTP stacks in the process. Consolidating
`client.py` onto `httpx2` is tracked as its own change, deliberately not bundled with the SDK
migration.

## References

- `src/ms_graph_mcp/client.py` — the whole Graph client, ~330 lines
- `pyproject.toml` — the `# NB:` note on the `dependencies` block
