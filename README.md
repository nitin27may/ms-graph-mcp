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

> **Not on PyPI yet.** `pip install ms-graph-mcp` and `uvx --from ms-graph-mcp` will not work until
> the first release. Run it from a clone — the Quick start below covers it, and every client config
> in this README uses that path.

Requires **Python 3.12+** and [uv](https://docs.astral.sh/uv/).

## Quick start

You need an **Entra ID app registration** — about two minutes. **Do not create a client secret:**
this registers as a *public client*, which signs you in through your browser using PKCE. A secret
on a program running on your own machine would be readable by anyone with the config file, which is
why the flow is designed not to need one. Nothing is pasted into a config file except two ids,
neither of which is sensitive.

### 1. Register the app

In the [Entra portal](https://entra.microsoft.com) → **App registrations** → **New registration**:

- **Name:** anything, e.g. `ms-graph-mcp`
- **Supported account types:** *Accounts in this organizational directory only*
- **Redirect URI:** select **Public client/native**, value `http://localhost`

Leave **Certificates & secrets** alone — you do not need anything from it.

Then, on the new app:

- **Authentication** → enable **Allow public client flows** (this permits device-code sign-in)
- **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions**, and
  add what you want the agent to reach. A sensible read-only starting set:

  ```
  User.Read  Mail.Read  Calendars.Read  Files.Read.All
  People.Read  Chat.Read  Tasks.Read  Notes.Read  Contacts.Read
  ```

  The complete, copy-paste consent set — and which permission each individual tool needs — is in
  [docs/permissions.md](docs/permissions.md).

Copy the **Application (client) ID** and **Directory (tenant) ID** from the Overview page.

### 2. Get the code and run it

The package is **not on PyPI yet**, so run it from a clone. Everything below assumes
`~/workspace/ms-graph-mcp` — substitute your own path.

```bash
git clone https://github.com/nitin27may/ms-graph-mcp ~/workspace/ms-graph-mcp
cd ~/workspace/ms-graph-mcp
uv sync                       # creates .venv and installs everything
```

That gives you:

```
~/workspace/ms-graph-mcp/
├── pyproject.toml            # defines the ms-graph-mcp console script
├── uv.lock
├── src/ms_graph_mcp/         # the server
├── tests/
├── docs/
└── .venv/                    # created by uv sync
```

Check it starts:

```bash
GRAPH_MCP_CLIENT_ID=<application-client-id> \
GRAPH_MCP_TENANT_ID=<directory-tenant-id> \
  uv run ms-graph-mcp
```

Your browser opens for Microsoft 365 sign-in. The process then sits waiting for a client to speak
MCP to it over stdin — that is correct; press Ctrl-C. The sign-in is cached in
`~/.ms-graph-mcp/token_cache.json` (owner-readable only), so it will not prompt again.

To run it from anywhere — which is what an MCP client needs, since it will not be launched from
this directory — use `--directory`:

```bash
uv run --directory ~/workspace/ms-graph-mcp ms-graph-mcp
```

**MCP clients need an absolute path.** They do not inherit your shell's working directory, and most
do not expand `~`. Get yours with:

```bash
cd ~/workspace/ms-graph-mcp && pwd
# /Users/you/workspace/ms-graph-mcp
```

## Add it to your client

Every example uses the local clone, because that is what works today. **Replace
`/Users/you/workspace/ms-graph-mcp` with your own absolute path** — the output of `pwd` in the
clone. After the first PyPI release you can swap the command for `uvx` (see the end of this
section).

### VS Code

Create `.vscode/mcp.json` in whichever workspace you want it available in:

```jsonc
{
  "inputs": [
    { "id": "clientId", "type": "promptString", "description": "Entra application (client) ID" },
    { "id": "tenantId", "type": "promptString", "description": "Entra directory (tenant) ID" }
  ],
  "servers": {
    "ms-graph": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--directory", "/Users/you/workspace/ms-graph-mcp",
        "ms-graph-mcp"
      ],
      "env": {
        "GRAPH_MCP_CLIENT_ID": "${input:clientId}",
        "GRAPH_MCP_TENANT_ID": "${input:tenantId}"
      }
    }
  }
}
```

Reload the window. VS Code prompts once for the two ids and remembers them, so this file is safe to
commit. Open the Chat view, switch to **Agent** mode, and the tools appear under the tools picker.
`MCP: List Servers` shows status and output if it does not connect.

For every workspace rather than one, run **MCP: Open User Configuration** and put the same
`servers` block there.

### Claude Code

```bash
claude mcp add ms-graph \
  --env GRAPH_MCP_CLIENT_ID=<application-client-id> \
  --env GRAPH_MCP_TENANT_ID=<directory-tenant-id> \
  -- uv run --directory /Users/you/workspace/ms-graph-mcp ms-graph-mcp
```

Then `/mcp` inside Claude Code to confirm it connected and see the tool list.

### Claude Desktop

`claude_desktop_config.json` — macOS `~/Library/Application Support/Claude/`,
Windows `%APPDATA%\Claude\`:

```jsonc
{
  "mcpServers": {
    "ms-graph": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/Users/you/workspace/ms-graph-mcp",
        "ms-graph-mcp"
      ],
      "env": {
        "GRAPH_MCP_CLIENT_ID": "<application-client-id>",
        "GRAPH_MCP_TENANT_ID": "<directory-tenant-id>"
      }
    }
  }
}
```

Restart Claude Desktop fully — quit it, do not just close the window.

### MCP Inspector

The quickest way to check the server independently of any client:

```bash
GRAPH_MCP_CLIENT_ID=… GRAPH_MCP_TENANT_ID=… \
  npx @modelcontextprotocol/inspector \
  uv run --directory /Users/you/workspace/ms-graph-mcp ms-graph-mcp
```

Needs Node 22.19+. It opens a browser UI where you can list tools and call them by hand — worth
doing before blaming your client.

### Two things that catch people out

**`uv` must be on the client's PATH.** GUI apps launched from Finder or the Dock do not inherit
your shell's PATH, so a client can fail to start the server with an unhelpful error. If that
happens, use the absolute path to `uv`:

```bash
which uv     # e.g. /Users/you/.local/bin/uv
```

and put that in `"command"` instead of `"uv"`.

**`--directory` is not optional.** Without it, `uv run` resolves against whatever directory the
client happened to launch from, which will not be the project.

### After the first PyPI release

Once published, no clone is needed — replace the command and args with:

```jsonc
"command": "uvx",
"args": ["--from", "ms-graph-mcp", "ms-graph-mcp"]
```

Everything else, including the `env` block, stays the same.

### stdio settings

| Env var | Purpose |
|---|---|
| `GRAPH_MCP_CLIENT_ID` | Entra application (client) id. Enables interactive sign-in. |
| `GRAPH_MCP_TENANT_ID` | Entra directory (tenant) id. Defaults to `common`. |
| `GRAPH_MCP_SCOPES` | Comma-separated delegated scopes to request. Defaults to a read-only set. |
| `GRAPH_MCP_TOOLSETS` | Which tool profiles to expose. Defaults to `core`. See below. |
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
| `AADSTS53003`, or "You cannot access this right now" **after** a successful sign-in | A Conditional Access policy requires a registered or compliant device. See below. |
| Client shows "server disconnected" | Run the same command in a terminal; startup errors go to stderr and the client usually hides them. |

#### AADSTS53003 — blocked by Conditional Access

Your credentials were accepted and the sign-in *succeeded*; a policy then refused the token. Click
**More details** on the error page and look at the device lines:

```
Error Code:        53003
Device platform:   macOS
Device state:      Unregistered      <- the cause
Device identifier: Not available
```

Your tenant requires a **registered or compliant device**, and a plain system browser has no device
identity to present. This is why Outlook and Teams still work: they sign in through the **Microsoft
Enterprise SSO plug-in** (shipped with Company Portal / Intune), which holds that identity.

**Nothing in the app registration fixes this.** Conditional Access is evaluated separately from app
configuration — not API permissions, not redirect URIs, not "allow public client flows". Your
registration is already fine; the sign-in got past it.

Two real fixes:

1. **Register the device.** Install Company Portal and sign in; device state becomes Registered and
   the policy is satisfied. Your existing configuration then works unchanged.
2. **Ask an admin to exclude the app.** Entra → Protection → Conditional Access → find the policy
   (Sign-in logs → the failed entry → **Conditional Access** tab names it) → exclude this
   application id, or your account.

Device-code sign-in is **not** a workaround — it fails the same device check, and many tenants block
that flow outright as a phishing vector.

## Streamable HTTP — hosted deployments

```bash
# from a clone
GRAPH_MCP_PORT=8094 uv run --directory /path/to/ms-graph-mcp ms-graph-mcp-http
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

All settings are read from the environment; a `.env` in the working directory is picked up.

**Which settings you need depends entirely on how you run it.** The two deployment shapes use
different authentication models, and mixing them up is the most common setup mistake.

### Running locally (stdio) — you are the user

The server signs *you* in. It is a **public client**, so there is **no client secret** — a program
running on your own machine cannot keep one, since anyone with the config file or the process has
it. MSAL uses PKCE instead.

| Setting | Env | Default |
|---|---|---|
| Application (client) id | `GRAPH_MCP_CLIENT_ID` / `AZURE_AD_CLIENT_ID` | `""` |
| Directory (tenant) id | `GRAPH_MCP_TENANT_ID` / `AZURE_AD_TENANT_ID` | `common` |
| Delegated scopes to request at sign-in | `GRAPH_MCP_SCOPES` | read-only set |
| Tool profiles to expose | `GRAPH_MCP_TOOLSETS` | `core` |
| Expose the write tools | `GRAPH_MCP_WRITE_SCOPE` | `false` |
| Caller identity, for tenant-scoping | `GRAPH_MCP_USER_EMAIL` | `""` |
| Always use device code, never the browser | `GRAPH_MCP_FORCE_DEVICE_CODE` | `false` |
| Where the token cache lives | `GRAPH_MCP_CACHE_DIR` | `~/.ms-graph-mcp` |
| Pre-acquired token instead of signing in (CI) | `GRAPH_MCP_ACCESS_TOKEN` | `""` |

> **`GRAPH_MCP_CLIENT_SECRET` is not used here and should not be set.** It belongs to the hosted
> shape below. If you find yourself creating a client secret to run this locally, something has
> gone wrong — the app registration only needs to be a public client with `http://localhost` as its
> redirect URI.

### Running hosted (Streamable HTTP) — the server acts for many users

Callers present a token; the server validates it and may exchange it. This is where a client secret
belongs, because the server is a confidential client running somewhere you control.

| Setting | Env | Default |
|---|---|---|
| Verify JWT signatures against JWKS | `GRAPH_MCP_JWT_VERIFY` | `true` |
| Shared secret for machine callers | `GRAPH_MCP_SHARED_SECRET` | `""` (no gate) |
| Server performs its own OBO exchange | `GRAPH_MCP_DOES_OBO` | `false` |
| Client secret, for the OBO exchange | `GRAPH_MCP_CLIENT_SECRET` / `AZURE_AD_CLIENT_SECRET` | `""` |
| Audience to validate in OBO mode | `GRAPH_MCP_AUDIENCE` | derived from client id |
| Graph scopes requested during OBO | `GRAPH_MCP_OBO_SCOPES` | `…/.default` |
| HTTP port | `GRAPH_MCP_PORT` | `8094` |

`GRAPH_MCP_CLIENT_ID` and `GRAPH_MCP_TENANT_ID` are needed in both shapes.

**`GRAPH_MCP_JWT_VERIFY` defaults on.** Turn it off only for a local run with no JWKS connectivity —
with it off, token signatures are not verified. There is deliberately no setting that skips
authentication altogether; see [ADR 0003](docs/adr/0003-no-gateway-trust-mode.md).

### Behaviour and safety

| Setting | Env | Default |
|---|---|---|
| Remove the write tier entirely | `GRAPH_MCP_READ_ONLY` | `false` |
| Recipient-domain allowlist for sending and forwarding mail | `GRAPH_MCP_SEND_EMAIL_ALLOWED_DOMAINS` | `""` (no gate) |
| Max files per browse | `GRAPH_MCP_BROWSE_MAX_FILES` | `500` |
| TLS verification off (corporate proxy) | `GRAPH_MCP_DISABLE_SSL_VERIFY` | `false` |

`GRAPH_MCP_READ_ONLY` is stronger than leaving `GRAPH_MCP_WRITE_SCOPE` off: it removes the write
tools from the deployment entirely, so no caller can reach them whatever they ask for. See
[SECURITY.md](SECURITY.md) for what to change before exposing this beyond localhost.

## Toolset profiles

85 tools is a lot to put in front of a model. `GRAPH_MCP_TOOLSETS` selects named profiles, each a
group of namespaces:

| Profile | Namespaces | Read tools | Approx. tokens |
|---|---|---:|---:|
| `core` *(default)* | search, mail, calendar, files, people | 23 | ~4,200 |
| `mail` | mail | 5 | ~800 |
| `calendar` | calendar | 6 | ~1,500 |
| `meetings` | meetings, calendar | 13 | ~2,900 |
| `files` | files | 6 | ~900 |
| `chat` | chat | 7 | ~1,000 |
| `people` | people | 5 | ~750 |
| `directory` | directory, people | 12 | ~1,900 |
| `tasks` | tasks | 5 | ~830 |
| `notes` | notes | 4 | ~570 |
| `search` | search | 1 | ~290 |
| `all` | everything | 53 | ~9,200 |

Combine them with commas:

```
GRAPH_MCP_TOOLSETS=mail,calendar,tasks
```

**`core` is the default, so some tools are not advertised unless you ask for them.** If you want
Teams chat, Planner, OneNote, meeting transcripts or directory lookups, name those profiles — or set
`GRAPH_MCP_TOOLSETS=all` to expose everything.

Over HTTP a caller may send `X-Toolsets` to narrow further for one request. **It can only narrow.**
The startup value is a ceiling, so a client asking for `all` gains nothing the deployment did not
already enable.

This filters *visibility*, not authority. A hidden tool is simply not listed; the write-scope and
internal-tier gates are what actually stop a call, and they are unaffected.

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
| [docs/permissions.md](docs/permissions.md) | Every tool and the delegated permission it needs, plus copy-paste consent sets |
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
