# ms-graph-mcp

[![CI](https://github.com/nitin27may/ms-graph-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/nitin27may/ms-graph-mcp/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue)](https://pypi.org/project/ms-graph-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-server-orange)](https://modelcontextprotocol.io)

A [Model Context Protocol](https://modelcontextprotocol.io) server for **Microsoft Graph** — 85
tools across mail, calendar, meetings (including transcripts), Teams chat, files, SharePoint,
people, contacts, directory, tasks and OneNote, over **stdio** or **Streamable HTTP**.

**Signs you in with your own Microsoft account** — browser SSO, no token to paste, no client
secret.

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

> **Not yet on PyPI.** The package name is unclaimed and the release pipeline is in place, but
> nothing has been published. Until the first release, install from source. The `uvx --from
> ms-graph-mcp` commands below are what will work after publication; substitute
> `--from git+https://github.com/nitin27may/ms-graph-mcp` today.

```bash
# From source (works now)
git clone https://github.com/nitin27may/ms-graph-mcp
cd ms-graph-mcp
uv sync

# After the first PyPI release
uv add ms-graph-mcp
# or
pip install ms-graph-mcp
```

## Quick start

You need an **Entra ID app registration** — it takes about two minutes, and no client secret is
involved. The server signs you in through your browser; no token is ever pasted into a config file.

### 1. Register the app

In the [Entra portal](https://entra.microsoft.com) → **App registrations** → **New registration**:

- **Name:** anything, e.g. `ms-graph-mcp`
- **Supported account types:** *Accounts in this organizational directory only*
- **Redirect URI:** select **Public client/native**, value `http://localhost`

Then, on the new app:

- **Authentication** → enable **Allow public client flows** (this permits device-code sign-in)
- **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions**, and
  add what you want the agent to reach. A sensible read-only starting set:

  ```
  User.Read  Mail.Read  Calendars.Read  Files.Read.All
  People.Read  Chat.Read  Tasks.Read  Notes.Read  Contacts.Read
  ```

Copy the **Application (client) ID** and **Directory (tenant) ID** from the Overview page.

### 2. Run it

```bash
GRAPH_MCP_CLIENT_ID=<application-client-id> \
GRAPH_MCP_TENANT_ID=<directory-tenant-id> \
  uvx --from ms-graph-mcp ms-graph-mcp
```

On first run your browser opens for normal Microsoft 365 sign-in — SSO, MFA and conditional access
all apply as usual. The result is cached in `~/.ms-graph-mcp/token_cache.json` (owner-readable
only), so later runs start silently. Over SSH or in a container it falls back to device-code
sign-in and prints a code to stderr.

## Add it to your client

The same three settings work everywhere. **No access token goes in these files.**

### VS Code

`.vscode/mcp.json` in your workspace, or **MCP: Open User Configuration** for all projects:

```jsonc
{
  "inputs": [
    { "id": "clientId", "type": "promptString", "description": "Entra application (client) ID" },
    { "id": "tenantId", "type": "promptString", "description": "Entra directory (tenant) ID" }
  ],
  "servers": {
    "ms-graph": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "ms-graph-mcp", "ms-graph-mcp"],
      "env": {
        "GRAPH_MCP_CLIENT_ID": "${input:clientId}",
        "GRAPH_MCP_TENANT_ID": "${input:tenantId}"
      }
    }
  }
}
```

The `inputs` block means VS Code prompts once and stores the values itself, so the file is safe to
commit. Hardcode the two ids instead if you prefer — neither is a secret.

### Claude Code

```bash
claude mcp add ms-graph \
  --env GRAPH_MCP_CLIENT_ID=<application-client-id> \
  --env GRAPH_MCP_TENANT_ID=<directory-tenant-id> \
  -- uvx --from ms-graph-mcp ms-graph-mcp
```

Then `/mcp` in Claude Code to check it connected.

### Claude Desktop

`claude_desktop_config.json` — macOS
`~/Library/Application Support/Claude/`, Windows `%APPDATA%\Claude\`:

```jsonc
{
  "mcpServers": {
    "ms-graph": {
      "command": "uvx",
      "args": ["--from", "ms-graph-mcp", "ms-graph-mcp"],
      "env": {
        "GRAPH_MCP_CLIENT_ID": "<application-client-id>",
        "GRAPH_MCP_TENANT_ID": "<directory-tenant-id>"
      }
    }
  }
}
```

### MCP Inspector

Useful for checking the server independently of any client:

```bash
GRAPH_MCP_CLIENT_ID=… GRAPH_MCP_TENANT_ID=… \
  npx @modelcontextprotocol/inspector uvx --from ms-graph-mcp ms-graph-mcp
```

### Running from a clone

Substitute the command while developing:

```jsonc
"command": "uv",
"args": ["run", "--directory", "/path/to/ms-graph-mcp", "ms-graph-mcp"]
```

### stdio settings

| Env var | Purpose |
|---|---|
| `GRAPH_MCP_CLIENT_ID` | Entra application (client) id. Enables interactive sign-in. |
| `GRAPH_MCP_TENANT_ID` | Entra directory (tenant) id. Defaults to `common`. |
| `GRAPH_MCP_SCOPES` | Comma-separated delegated scopes to request. Defaults to a read-only set. |
| `GRAPH_MCP_WRITE_SCOPE` | `true` to expose the 23 write tools. **Default off.** |
| `GRAPH_MCP_USER_EMAIL` | Caller identity, used for tenant-scoping in some tools. Optional. |
| `GRAPH_MCP_ACCESS_TOKEN` | A pre-acquired delegated token, instead of signing in. For CI. |
| `GRAPH_MCP_FORCE_DEVICE_CODE` | `true` to skip the browser and always use device-code sign-in. |

**Turning on write tools.** They are off by default: with them enabled an agent can send mail,
book meetings and change files as you. To use them, add the matching write scopes to
`GRAPH_MCP_SCOPES` *and* set `GRAPH_MCP_WRITE_SCOPE=true`:

```
GRAPH_MCP_SCOPES=User.Read,Mail.ReadWrite,Mail.Send,Calendars.ReadWrite,Files.ReadWrite.All,Tasks.ReadWrite,ChatMessage.Send
GRAPH_MCP_WRITE_SCOPE=true
```

### Troubleshooting

| Symptom | Cause |
|---|---|
| `AADSTS7000218` on sign-in | **Allow public client flows** is not enabled on the app registration. |
| `AADSTS50011` redirect mismatch | The redirect URI is not `http://localhost`, or the platform is not *Public client/native*. |
| `SCOPE_DENIED` from a tool | The permission that tool needs was not consented. The error names it — add it in **API permissions** and sign in again. |
| Sign-in prompts every time | The cache at `~/.ms-graph-mcp/token_cache.json` is not writable. |
| Browser never opens | Expected over SSH or in containers — use the device code printed to stderr. |
| Client shows "server disconnected" | Run the same command in a terminal; startup errors go to stderr and the client usually hides them. |

## Streamable HTTP — hosted deployments

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

events = await calendar.calendar_list_upcoming_events(params, {"access_token": tok})
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
| Remove the write tier entirely | `GRAPH_MCP_READ_ONLY` | `false` |
| Shared secret for machine callers | `GRAPH_MCP_SHARED_SECRET` | `""` (no gate) |
| Tenant id | `GRAPH_MCP_TENANT_ID` / `AZURE_AD_TENANT_ID` | `""` |
| Client id | `GRAPH_MCP_CLIENT_ID` / `AZURE_AD_CLIENT_ID` | `""` |
| Client secret | `GRAPH_MCP_CLIENT_SECRET` / `AZURE_AD_CLIENT_SECRET` | `""` |
| Verify JWT signatures (JWKS) | `GRAPH_MCP_JWT_VERIFY` | `true` |
| Server performs its own OBO exchange | `GRAPH_MCP_DOES_OBO` | `false` |
| Audience to validate in OBO mode | `GRAPH_MCP_AUDIENCE` | derived from client id |
| Graph scopes requested during OBO | `GRAPH_MCP_OBO_SCOPES` | `https://graph.microsoft.com/.default` |
| HTTP port | `GRAPH_MCP_PORT` | `8094` |

**`GRAPH_MCP_JWT_VERIFY` defaults on.** Turn it off only for a local run with no JWKS connectivity —
with it off, token signatures are not verified. There is deliberately no setting that skips
authentication altogether; see [ADR 0003](docs/adr/0003-no-gateway-trust-mode.md).

## Tool surface

Three tiers, one auth seam.

| Tier | Count | Exposed when | Examples |
|---|---:|---|---|
| **Read** | 53 | always | `calendar_list_upcoming_events`, `mail_search`, `meetings_get_transcript`, `files_search`, `directory_list_user_groups` |
| **Write** | 23 | `X-Write-Scope: true` | `mail_send`, `files_upload`, `files_create_sharing_link`, `notes_create_page`, `tasks_create_todo` |
| **Internal** | 9 | `X-Internal-Scope: true`, machine principal only | `graph_request` passthrough, drive walk/upload, message attachments, app-only `probe_graph_access` |

Tool names are namespaced by Graph permission family rather than by Microsoft product, because real
questions cross product boundaries — `files_` covers OneDrive *and* SharePoint document libraries,
which are the same `driveItem` resource underneath.

By namespace: mail 11 · tasks 11 · calendar 10 · files 10 · chat 8 · directory 7 · meetings 7 ·
people 6 · notes 5 · search 1.

Every tool declares MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`) so clients
know what needs confirming, and every description names the delegated permission it requires.

> **Renamed in 0.2.0.** Every pre-0.2.0 tool name still works as an alias and will keep working
> until 0.3.0. Aliases are accepted by `tools/call` but are not advertised in `tools/list`.

The lists live in `ms_graph_mcp.allowlists` and are resolved against the tool registry on every
`tools/list`. A name in an allowlist with no registered tool raises rather than being skipped — the
server refuses to serve a partial surface instead of silently dropping a tool.

## Documentation

| | |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup, the add-a-tool checklist, and the invariants that are enforced by tests |
| [SECURITY.md](SECURITY.md) | Reporting vulnerabilities, and the settings to change before exposing this beyond localhost |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [CLAUDE.md](CLAUDE.md) | Architecture and the non-obvious traps, for coding agents and new contributors alike |
| [docs/graph-coverage.md](docs/graph-coverage.md) | What this server covers of the Graph v1.0 surface, what it does not, and what is out of scope |
| [docs/adr/](docs/adr/) | Architecture Decision Records |

## Development

```bash
uv sync
uv run pytest -q            # full suite
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
