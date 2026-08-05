# Documentation

Start at the [README](../README.md) — install, app registration, and client configuration are there.
These are the reference pages behind it.

## Using it

| | |
|---|---|
| [configuration.md](configuration.md) | Every environment variable, split by deployment shape. Local stdio and hosted HTTP use different authentication models. |
| [permissions.md](permissions.md) | Every tool and the delegated permission it needs, plus copy-paste consent sets. Generated from the tool descriptions and checked in CI. |
| [hosting.md](hosting.md) | Streamable HTTP, per-request headers, Docker and GHCR, and the `421` that catches every first deployment. |
| [agent-auth.md](agent-auth.md) | How an agent acting for a signed-in user reaches Graph through this server — the two-hop on-behalf-of chain, Entra Agent ID, scopes and Conditional Access step-up. |
| [troubleshooting.md](troubleshooting.md) | Setup failures — Entra errors, Conditional Access, corporate TLS proxies. |
| [debugging.md](debugging.md) | Logs, error codes, and the auth failures people actually hit once it is running. |

## Understanding it

| | |
|---|---|
| [graph-coverage.md](graph-coverage.md) | What this covers of the Graph v1.0 surface, workload by workload — what it does not, and what is out of scope. |
| [roadmap.md](roadmap.md) | What is not done. Shipped work lives in the [changelog](../CHANGELOG.md). |
| [adr/](adr/) | Architecture Decision Records — the choices that would otherwise be re-litigated. |

## Contributing to it

| | |
|---|---|
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Dev setup, the add-a-tool checklist, the invariants enforced by tests, and the release process. |
| [testing.md](testing.md) | Running the suite, how it is arranged, and driving the server with MCP Inspector. |
| [CLAUDE.md](../CLAUDE.md) | Architecture and the non-obvious traps, for coding agents and new contributors alike. |
| [SECURITY.md](../SECURITY.md) | Reporting vulnerabilities, and what to change before exposing this beyond localhost. |

## Architecture Decision Records

| | |
|---|---|
| [0001](adr/0001-src-layout.md) | Why the package lives under `src/`. |
| [0002](adr/0002-raw-httpx-graph-client.md) | Why the Graph client is raw `httpx`, and why `msgraph-sdk` and `azure-identity` are not dependencies. |
| [0003](adr/0003-no-gateway-trust-mode.md) | Why token validation always runs in-server, and why no gateway-trust bypass will be added. |
| [0004](adr/0004-resource-server-by-default.md) | Why the HTTP transport performs its own on-behalf-of exchange by default, and why that had to move off the shared dispatch path. |
