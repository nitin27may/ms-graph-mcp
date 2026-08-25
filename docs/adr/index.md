# Architecture Decision Records

The choices that would otherwise be re-litigated every few months — what was decided, what the
alternatives were, and what each one costs.

| | |
|---|---|
| [ADR 0001](0001-src-layout.md) | Why the package lives under `src/`. |
| [ADR 0002](0002-raw-httpx-graph-client.md) | Why the Graph client is raw `httpx`, and why `msgraph-sdk` and `azure-identity` are not dependencies. |
| [ADR 0003](0003-no-gateway-trust-mode.md) | Why token validation always runs in-server, and why no gateway-trust bypass will be added. |
