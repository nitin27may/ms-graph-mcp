# ms-graph-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server for **Microsoft Graph** — 85
tools across mail, calendar, meetings (including transcripts), Teams chat, files, SharePoint,
search, people, contacts, directory, tasks and OneNote, over **stdio** or **Streamable HTTP**.

**Signs you in with your own Microsoft account** — browser SSO, no token to paste, no client secret.

!!! info "Status: early"
    Extracted from a production agent platform where it has been running against a real tenant.
    The code is battle-tested; the packaging and public API surface are newer. Expect the config
    surface to move before 1.0.

## What makes it different

- **No `msgraph-sdk`, no `azure-identity`** — the Graph client is raw `httpx`, so the dependency
  tree stays small and the wire behaviour is inspectable.
- **Read/write separation is enforced, not advisory** — write tools are hidden *and* refused unless
  the caller explicitly opts in.
- **Auth-agnostic by default** — tools receive an already-acquired Graph token via the request
  context. The server can also perform its own on-behalf-of exchange when you want it to act as a
  proper OAuth resource server.

## Quick start

Requires **Python 3.12+**. [uv](https://docs.astral.sh/uv/) runs it straight from PyPI — nothing to
clone:

```bash
uvx --from ms-graph-mcp ms-graph-mcp          # stdio, for an MCP client
uvx --from ms-graph-mcp ms-graph-mcp-http     # Streamable HTTP
```

You need two values from an Entra ID app registration — an application (client) id and a directory
(tenant) id. Neither is sensitive, and you should **not** create a client secret: the server
registers as a public client and signs you in through the browser using PKCE.

```bash
GRAPH_MCP_CLIENT_ID=<application-client-id> \
GRAPH_MCP_TENANT_ID=<directory-tenant-id> \
  uvx --from ms-graph-mcp ms-graph-mcp
```

The [project README](https://github.com/nitin27may/ms-graph-mcp/blob/main/README.md) walks through the app registration and carries ready-made config
blocks for VS Code, Claude Code, Claude Desktop, Cursor, Windsurf and MCP Inspector. Everything
past that first run is documented here.

## Where to go next

### Using it

| | |
|---|---|
| [Configuration](configuration.md) | Every environment variable, split by deployment shape. Local stdio and hosted HTTP use different authentication models. |
| [Delegated permissions](permissions.md) | Every tool and the delegated permission it needs, plus copy-paste consent sets. Generated from the tool descriptions and checked in CI. |
| [Hosting](hosting.md) | Streamable HTTP, per-request headers, Docker and GHCR, and the `421` that catches every first deployment. |
| [Troubleshooting](troubleshooting.md) | Setup failures — Entra errors, Conditional Access, corporate TLS proxies. |
| [Debugging](debugging.md) | Logs, error codes, and the auth failures people actually hit once it is running. |

### Understanding it

| | |
|---|---|
| [Graph coverage](graph-coverage.md) | What this covers of the Graph v1.0 surface, workload by workload — what it does not, and what is out of scope. |
| [Roadmap](roadmap.md) | What is not done. Shipped work lives in the [changelog](https://github.com/nitin27may/ms-graph-mcp/blob/main/CHANGELOG.md). |
| [Design decisions](adr/index.md) | Architecture Decision Records — the choices that would otherwise be re-litigated. |

### Contributing to it

| | |
|---|---|
| [Contributing](https://github.com/nitin27may/ms-graph-mcp/blob/main/CONTRIBUTING.md) | Dev setup, the add-a-tool checklist, the invariants enforced by tests, and the release process. |
| [Testing](testing.md) | Running the suite, how it is arranged, and driving the server with MCP Inspector. |
| [CLAUDE.md](https://github.com/nitin27may/ms-graph-mcp/blob/main/CLAUDE.md) | Architecture and the non-obvious traps, for coding agents and new contributors alike. |
| [Security](https://github.com/nitin27may/ms-graph-mcp/blob/main/SECURITY.md) | Reporting vulnerabilities, and what to change before exposing this beyond localhost. |
