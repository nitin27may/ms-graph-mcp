# ms-graph-mcp

[![CI](https://github.com/nitin27may/ms-graph-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/nitin27may/ms-graph-mcp/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)](https://pypi.org/project/ms-graph-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-server-orange)](https://modelcontextprotocol.io)

A [Model Context Protocol](https://modelcontextprotocol.io) server for **Microsoft Graph** — 55
tools across calendar, email, meetings (including transcripts), Teams, files, people, directory,
tasks and OneNote, over **stdio** or **Streamable HTTP**.

- **No `msgraph-sdk`, no `azure-identity`** — the Graph client is raw `httpx`, so the dependency
  tree stays small and the wire behaviour is inspectable.
- **Auth-agnostic by default** — tools receive an already-acquired Graph token via the request
  context. The server can also perform its own on-behalf-of exchange when you want it to act as a
  proper OAuth resource server.
- **Read/write separation is enforced, not advisory** — write tools are hidden and refused unless
  the caller explicitly opts in.

> **Status: early.** Extracted from a production agent platform where it has been running against a
> real tenant. The code is battle-tested; the packaging, docs, and public API surface are new.
> Expect the config surface to move before 1.0.

## Install

```bash
uv add ms-graph-mcp
# or
pip install ms-graph-mcp
```

## Run

### stdio — Claude Desktop, Claude Code, any local MCP client

```bash
GRAPH_MCP_ACCESS_TOKEN=<a delegated Microsoft Graph token> \
  uvx --from ms-graph-mcp ms-graph-mcp
```

```jsonc
// claude_desktop_config.json
{
  "mcpServers": {
    "ms-graph": {
      "command": "uvx",
      "args": ["--from", "ms-graph-mcp", "ms-graph-mcp"],
      "env": {
        "GRAPH_MCP_ACCESS_TOKEN": "<delegated graph token>",
        "GRAPH_MCP_USER_EMAIL": "you@yourtenant.com"
      }
    }
  }
}
```

| stdio env var | Purpose |
|---|---|
| `GRAPH_MCP_ACCESS_TOKEN` | The delegated Graph token every tool call uses. |
| `GRAPH_MCP_USER_EMAIL` | Caller identity, used for tenant-scoping in some tools. |
| `GRAPH_MCP_WRITE_SCOPE` | `true` to expose the 4 write tools. Default off. |

### Streamable HTTP

```bash
GRAPH_MCP_PORT=8094 uvx --from ms-graph-mcp ms-graph-mcp-http
curl -s localhost:8094/health
```

Per-request headers:

| Header | Purpose |
|---|---|
| `Authorization: Bearer <token>` | **Required.** Either a Microsoft Graph access token (validated as a real Entra JWT), or the configured shared secret for a machine caller. |
| `X-Write-Scope: true` | Expose *and* permit the write tools for this request. |
| `X-Entra-App-Token: <token>` | Optional app-only token for directory/group lookups that delegated permissions can't cover tenant-wide. |
| `X-Internal-Scope: true` | Expose the internal deterministic tier. Honoured **only** for the shared-secret machine principal — never for a user token. |
| `X-OBO-Token: <token>` | Internal tier only: an explicitly supplied downstream token. |

### Embed in your own app

```python
from ms_graph_mcp.app import build_app
from ms_graph_mcp.config import GraphMcpConfig

app = build_app(GraphMcpConfig(shared_secret="…"))  # a Starlette app — mount or serve it
```

`build_app(cfg, *, setup_telemetry=None, instrument_starlette=None)` takes optional OpenTelemetry
hooks. The domain modules also work as plain async functions, without MCP:

```python
from ms_graph_mcp import calendar

events = await calendar.get_upcoming_meetings(params, {"access_token": tok})
```

## Configuration

All settings are read from the environment (a `.env` in the working directory is picked up).
`GRAPH_MCP_*` is canonical; the three app-registration fields also accept the conventional
`AZURE_AD_*` names.

| Setting | Env | Default |
|---|---|---|
| TLS verification off (corporate proxy) | `GRAPH_MCP_DISABLE_SSL_VERIFY` | `false` |
| Recipient-domain allowlist for send/propose email | `GRAPH_MCP_SEND_EMAIL_ALLOWED_DOMAINS` | `""` (no gate) |
| Max files per browse | `GRAPH_MCP_BROWSE_MAX_FILES` | `500` |
| Shared secret for machine callers | `GRAPH_MCP_SHARED_SECRET` | `""` (no gate) |
| Tenant id | `GRAPH_MCP_TENANT_ID` / `AZURE_AD_TENANT_ID` | `""` |
| Client id | `GRAPH_MCP_CLIENT_ID` / `AZURE_AD_CLIENT_ID` | `""` |
| Client secret | `GRAPH_MCP_CLIENT_SECRET` / `AZURE_AD_CLIENT_SECRET` | `""` |
| Verify JWT signatures (JWKS) | `GRAPH_MCP_JWT_VERIFY` | `false` |
| Server performs its own OBO exchange | `GRAPH_MCP_DOES_OBO` | `false` |
| Audience to validate in OBO mode | `GRAPH_MCP_AUDIENCE` | derived from client id |
| Graph scopes requested during OBO | `GRAPH_MCP_OBO_SCOPES` | `https://graph.microsoft.com/.default` |
| HTTP port | `GRAPH_MCP_PORT` | `8094` |

**Turn `GRAPH_MCP_JWT_VERIFY` on for anything reachable beyond localhost.** It defaults off so a
local stdio run works without JWKS connectivity.

## Tool surface

Three tiers, one auth seam.

| Tier | Count | Exposed when | Examples |
|---|---:|---|---|
| **Read** | 42 | always | `get_upcoming_meetings`, `search_emails`, `get_meeting_transcript`, `search_files`, `get_user_groups` |
| **Write** | 4 | `X-Write-Scope: true` | `send_email`, `propose_email`, `save_to_onenote`, `create_todo_task` |
| **Internal** | 9 | `X-Internal-Scope: true`, machine principal only | `graph_request` passthrough, drive walk/upload, message attachments, app-only `probe_graph_access` |

By domain: meetings 7 · directory 7 · email 6 · files 6 · tasks 6 · calendar 4 · Teams 4 ·
people 3 · OneNote 3.

The lists live in `ms_graph_mcp.allowlists` and are validated at startup — the server refuses to
start half-wired rather than silently serving a partial surface.

## Documentation

| | |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup, the add-a-tool checklist, and the invariants that are enforced by tests |
| [SECURITY.md](SECURITY.md) | Reporting vulnerabilities, and the settings to change before exposing this beyond localhost |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [CLAUDE.md](CLAUDE.md) | Architecture and the non-obvious traps, for coding agents and new contributors alike |
| [docs/adr/](docs/adr/) | Architecture Decision Records |

## Development

```bash
uv sync
uv run pytest -q            # 548 tests
uv run ruff check .
uv run ruff format .
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request — the tool allowlists and the
tier separation have invariants that are enforced rather than advisory.

## Roadmap

Tracked in more detail in the issues. The near-term programme:

- [x] **MCP SDK 2.x** — done. Speaks the 2026-07-28 protocol revision while still serving 2025-era
      clients. Note that 2.x moves the SDK's HTTP stack to `httpx2`, a distribution separate from the
      `httpx` this server's Graph client uses; consolidating the two is tracked separately.
- [ ] **OAuth resource server** — RFC 9728 Protected Resource Metadata, `WWW-Authenticate`
      challenges, and RFC 8707 audience binding, so any spec-compliant MCP client can authenticate
      without client-specific configuration.
- [ ] **Toolset profiles** — expose a subset of the 55 tools per client, to cut the tool-definition
      tokens an agent pays before it does any work.
- [ ] **Graph coverage** — SharePoint sites and lists, unified `/search/query`, calendar write, and
      1:1 chats are the notable gaps.
- [ ] Full documentation: app-registration setup, the delegated-permission matrix per tool, and
      end-to-end configuration for VS Code, Claude Code, Claude Desktop and MCP Inspector.
- [ ] Sovereign cloud support (GCC High / 21Vianet) — a few Graph and login endpoints are still
      hardcoded to the commercial cloud.
- [ ] Publish to PyPI, and a container image on GHCR.

## Contributing

Issues and pull requests are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md); participation is
governed by the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

MIT — see [LICENSE](LICENSE).
