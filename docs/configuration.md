# Configuration

Every setting is read from the environment. A `.env` file in the working directory is picked up if
present; [`.env.example`](../.env.example) is a commented copy of everything below.

The three app-registration fields also accept the conventional `AZURE_AD_*` names, so an existing
Azure environment drives the server without renaming anything:

```
GRAPH_MCP_TENANT_ID      or  AZURE_AD_TENANT_ID
GRAPH_MCP_CLIENT_ID      or  AZURE_AD_CLIENT_ID
GRAPH_MCP_CLIENT_SECRET  or  AZURE_AD_CLIENT_SECRET
```

**Which settings you need depends entirely on how you run it.** The two deployment shapes use
different authentication models, and mixing them up is the most common setup mistake.

---

## Running locally (stdio) — you are the user

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

The default scope set is deliberately read-only — a first run should not consent to sending mail on
your behalf:

```
User.Read,Mail.Read,Calendars.Read,Files.Read.All,People.Read,Chat.Read,Tasks.Read,Notes.Read,Contacts.Read
```

> **`GRAPH_MCP_CLIENT_SECRET` is not used here and should not be set.** It belongs to the hosted
> shape below. If you find yourself creating a client secret to run this locally, something has gone
> wrong — the app registration only needs to be a public client with `http://localhost` as its
> redirect URI.

### Turning on write tools

They are off by default: with them enabled an agent can send mail, book meetings and change files as
you. Two things are required, and the scopes alone are not enough — add the matching write scopes to
`GRAPH_MCP_SCOPES` **and** set `GRAPH_MCP_WRITE_SCOPE=true`:

```
GRAPH_MCP_SCOPES=User.Read,Mail.Read,Mail.ReadWrite,Mail.Send,Calendars.ReadWrite,Files.ReadWrite.All,Tasks.ReadWrite,ChatMessage.Send,Notes.Create,Contacts.ReadWrite
GRAPH_MCP_WRITE_SCOPE=true
```

The complete consent sets are in [permissions.md](permissions.md). After changing scopes, delete
`~/.ms-graph-mcp/token_cache.json` to force a fresh consent — the cached token carries only what was
originally granted.

---

## Running hosted (Streamable HTTP) — the server acts for many users

Callers present a token; the server validates it and may exchange it. This is where a client secret
belongs, because the server is a confidential client running somewhere you control. See
[hosting.md](hosting.md) for the deployment side.

| Setting | Env | Default |
|---|---|---|
| Verify JWT signatures against JWKS | `GRAPH_MCP_JWT_VERIFY` | `true` |
| Shared secret for machine callers | `GRAPH_MCP_SHARED_SECRET` | `""` (no gate) |
| Server performs its own OBO exchange | `GRAPH_MCP_DOES_OBO` | **`true`** |
| Certificate (PEM bundle) for the exchange | `GRAPH_MCP_CLIENT_CERT_PATH` | `""` |
| Certificate passphrase | `GRAPH_MCP_CLIENT_CERT_PASSWORD` | `""` |
| Federated token file (workload identity) | `GRAPH_MCP_FEDERATED_TOKEN_FILE` / `AZURE_FEDERATED_TOKEN_FILE` | `""` |
| Client secret, for the exchange | `GRAPH_MCP_CLIENT_SECRET` / `AZURE_AD_CLIENT_SECRET` | `""` |
| Audience to validate | `GRAPH_MCP_AUDIENCE` | derived from client id |
| Graph scopes requested during OBO | `GRAPH_MCP_OBO_SCOPES` | `https://graph.microsoft.com/.default` |
| Permitted calling applications (`azp`) | `GRAPH_MCP_ALLOWED_AZP` | `""` (audience is the gate) |
| Delegated scope required to call at all | `GRAPH_MCP_REQUIRED_SCOPE` | `""` (no gate) |
| Delegated scope required for write tools | `GRAPH_MCP_WRITE_SCOPE_NAME` | `""` (header only) |
| HTTP port | `GRAPH_MCP_PORT` | `8094` |
| Public URL, enabling OAuth discovery | `GRAPH_MCP_RESOURCE_URL` | `""` (discovery off) |
| Additional accepted `Host` values | `GRAPH_MCP_ALLOWED_HOSTS` | `""` |

`GRAPH_MCP_CLIENT_ID` and `GRAPH_MCP_TENANT_ID` are needed in both shapes.

> **`GRAPH_MCP_JWT_VERIFY` defaults on.** Turn it off only for a local run with no JWKS connectivity
> — with it off, token *signatures* are not verified. The audience gate still runs: it needs no
> signing key, and it is what stops a token minted for another service being accepted here. There is
> deliberately no setting that skips authentication altogether; see
> [ADR 0003](adr/0003-no-gateway-trust-mode.md).

### The two auth postures

Selected by `GRAPH_MCP_DOES_OBO`. **This is an HTTP-transport setting and has no effect on stdio**,
where the server signs the user in directly and there is nothing to exchange.

- **Resource server (default).** The inbound token is audienced to this MCP. Audience binding is the
  gate, and the server exchanges the token for a Graph token via the on-behalf-of flow before the
  tool runs. Needs a confidential-client credential — see below. This is the posture the MCP
  authorization specification describes, and the one an agent should use.
- **Passthrough** (`GRAPH_MCP_DOES_OBO=false`, **deprecated**, removal in 1.0). The caller forwards
  an already-exchanged Graph token, validated for the Graph audience *plus* `azp == our client_id`.
  It is deprecated because a Graph-audienced token was not issued for this server, which the spec
  requires rejecting, and because a relayed token cannot satisfy Conditional Access claim step-up.
  See [ADR 0004](adr/0004-resource-server-by-default.md).

[agent-auth.md](agent-auth.md) walks through the app registrations, scopes and consent the default
posture needs.

### Credentials for the exchange

Precedence is **certificate → federated → secret**, so a leftover secret in the environment cannot
outrank a certificate configured deliberately.

| Setting | Use |
|---|---|
| `GRAPH_MCP_CLIENT_CERT_PATH` | Production. PEM bundle: `openssl pkcs12 -in file.pfx -out file.pem -nodes` |
| `GRAPH_MCP_FEDERATED_TOKEN_FILE` | AKS workload identity. Re-read per exchange, so rotation needs no restart |
| `GRAPH_MCP_CLIENT_SECRET` | Development. Warns at startup |

Microsoft's guidance is that client secrets "shouldn't be used as client credentials in production
environments". **The HTTP transport refuses to start** in resource-server mode with no credential,
rather than failing later on an arbitrary tool call.

### Binding the tool surface to token scopes

Audience binding proves a token was issued for this server. It says nothing about what its bearer
was granted, so without a scope gate any correctly-audienced token reaches everything.

```
GRAPH_MCP_REQUIRED_SCOPE=access_as_user
GRAPH_MCP_WRITE_SCOPE_NAME=access_as_user.write
```

With the second set, write access becomes `X-Write-Scope: true` **AND** the write scope present in
the token's `scp`. The header can then only narrow — an agent that was never granted the scope
cannot reach `mail_send` however it sets its headers. Left unset, the header alone decides, which is
the previous behaviour and why it is the default for now; the defaults become non-empty in 0.5.0.

Configuring either gate forces signature verification on, regardless of `GRAPH_MCP_JWT_VERIFY` — an
authorization check over an unverified token is forgeable, and a forgeable gate is worse than none
because it reads as protection.

---

## Behaviour and safety

| Setting | Env | Default |
|---|---|---|
| Remove the write tier entirely | `GRAPH_MCP_READ_ONLY` | `false` |
| Recipient-domain allowlist for sending and forwarding mail | `GRAPH_MCP_SEND_EMAIL_ALLOWED_DOMAINS` | `""` (no gate) |
| Max files per browse | `GRAPH_MCP_BROWSE_MAX_FILES` | `500` |
| Log level (`INFO` shows every Graph call) | `GRAPH_MCP_LOG_LEVEL` | `WARNING` |
| TLS verification off (corporate proxy) | `GRAPH_MCP_DISABLE_SSL_VERIFY` | `false` |

**`GRAPH_MCP_READ_ONLY` is stronger than leaving `GRAPH_MCP_WRITE_SCOPE` off.** It removes the write
tools from the deployment entirely, so no caller can reach them whatever they ask for — it is
enforced at dispatch, not just in `tools/list`. Hiding a tool is a context-efficiency measure; a
caller can still name any tool it likes.

**`GRAPH_MCP_SEND_EMAIL_ALLOWED_DOMAINS` covers `mail_send` and `mail_forward`** — the two tools
where the caller chooses the recipients. `mail_reply` and `mail_reply_all` are not gated, because
the thread already fixes who they go to. The check runs before the Graph call, not after.

**`GRAPH_MCP_DISABLE_SSL_VERIFY` is a corporate-proxy escape hatch, not a posture.** See
[troubleshooting.md](troubleshooting.md#ssl-certificate-verify-failures-behind-corporate-proxies).

See [SECURITY.md](../SECURITY.md) for what to change before exposing this beyond localhost.

---

## Toolset profiles

85 tools is a lot to put in front of a model. `GRAPH_MCP_TOOLSETS` selects named profiles, each a
group of namespaces. The table and the per-request `X-Toolsets` header are documented in the
[README](../README.md#toolset-profiles).

Two properties worth restating here:

- **The startup value is a ceiling.** `X-Toolsets` can narrow it for one request and can never widen
  it, which is what makes the header safe to honour from an untrusted caller.
- **This filters visibility, not authority.** A hidden tool is simply not listed. The write-scope and
  internal-tier gates are what actually stop a call, and they are unaffected.

An unknown profile name raises at startup rather than being ignored — silently skipping a typo would
serve a surface nobody asked for, with no signal that the configuration did not take effect.

---

## Embedding in your own app

`build_app()` is a factory returning a Starlette application:

```python
from ms_graph_mcp.app import build_app
from ms_graph_mcp.config import GraphMcpConfig

app = build_app(GraphMcpConfig(shared_secret="…"))  # mount it, or serve it
```

`GraphMcpConfig` accepts field names as well as the env aliases, so an embedding app can pass
settings directly. `build_app(cfg, *, setup_telemetry=None, instrument_starlette=None)` takes
optional OpenTelemetry hooks.

The domain modules also work as plain async functions, without MCP at all:

```python
from ms_graph_mcp import calendar

events = await calendar.calendar_list_upcoming_events(params, {"access_token": token})
```

Every tool has the same shape — `async def name(params: SomeBaseModel, context: dict)` — and the
`context` dict is the only channel between the transport's auth and the tool.
