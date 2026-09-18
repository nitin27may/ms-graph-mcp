# Configuration

Every setting is read from the environment. A `.env` file in the working directory is picked up if
present; [`.env.example`](https://github.com/nitin27may/ms-graph-mcp/blob/main/.env.example) is a commented copy of everything below.

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
| Server performs its own OBO exchange | `GRAPH_MCP_DOES_OBO` | `true` |
| Restrict which caller apps (`azp`) may call | `GRAPH_MCP_ALLOWED_AZP` | `""` (any) |
| Certificate (PEM) for the OBO exchange | `GRAPH_MCP_CLIENT_CERT_PATH` | `""` |
| Passphrase, if the private key is encrypted | `GRAPH_MCP_CLIENT_CERT_PASSPHRASE` | `""` |
| Federated token file (AKS workload identity) | `GRAPH_MCP_FEDERATED_TOKEN_FILE` / `AZURE_FEDERATED_TOKEN_FILE` | `""` |
| Client secret, for the OBO exchange | `GRAPH_MCP_CLIENT_SECRET` / `AZURE_AD_CLIENT_SECRET` | `""` |
| Audience to validate in OBO mode | `GRAPH_MCP_AUDIENCE` | derived from client id |
| Graph scopes requested during OBO | `GRAPH_MCP_OBO_SCOPES` | `https://graph.microsoft.com/.default` |
| Delegated scopes every caller must present | `GRAPH_MCP_REQUIRED_SCOPES` | `""` (no gate) |
| Delegated scope authorising the write tier | `GRAPH_MCP_WRITE_SCOPE_NAME` | `access_as_user.write` |
| HTTP port | `GRAPH_MCP_PORT` | `8094` |
| Public URL, enabling OAuth discovery | `GRAPH_MCP_RESOURCE_URL` | `""` (discovery off) |
| Additional accepted `Host` values | `GRAPH_MCP_ALLOWED_HOSTS` | `""` |

`GRAPH_MCP_CLIENT_ID` and `GRAPH_MCP_TENANT_ID` are needed in both shapes.

> **`GRAPH_MCP_JWT_VERIFY` defaults on.** Turn it off only for a local run with no JWKS connectivity
> — with it off, token signatures are not verified. There is deliberately no setting that skips
> authentication altogether; see [ADR 0003](adr/0003-no-gateway-trust-mode.md).

> **Configuring `GRAPH_MCP_REQUIRED_SCOPES` forces signature verification on**, whatever
> `GRAPH_MCP_JWT_VERIFY` says. A scope check over an unverified token is forgeable, and a gate that
> can be bypassed by forging a claim is worse than no gate at all — it reads as protection.

### Client credentials

The server authenticates as itself for the OBO exchange. Three ways, tried in this order:

| Credential | Setting | Use |
|---|---|---|
| Certificate | `GRAPH_MCP_CLIENT_CERT_PATH` | Production |
| Federated identity | `GRAPH_MCP_FEDERATED_TOKEN_FILE` | Production on AKS (workload identity) |
| Client secret | `GRAPH_MCP_CLIENT_SECRET` | Development |

Microsoft's guidance is explicit that client secrets
[shouldn't be used in production](https://learn.microsoft.com/en-us/entra/agent-id/agent-on-behalf-of-oauth-flow);
prefer a federated credential with a managed identity, or a certificate. Starting with a secret logs
a warning, but it still works — it is the only one of the three that works on a laptop.

The certificate file is a **PEM bundle holding the private key and its certificate**. PKCS#12 is not
read directly; convert it once:

```bash
openssl pkcs12 -in cert.pfx -out cert.pem -nodes
```

A rotated certificate needs a restart. A rotated federated token does not — the file is re-read on
demand, which matters because AKS refreshes the projected token roughly hourly.

**The server refuses to start** when `GRAPH_MCP_DOES_OBO` is on and no credential (or no tenant or
client id) is configured. Failing at boot rather than on the first tool call is deliberate: the
alternative is a deployment that passes its readiness probe, serves `tools/list`, and only breaks
when a user tries to do something.

### Write authority

In the resource-server posture, reaching a write tool needs **both**:

- `X-Write-Scope: true` on the request, and
- the `GRAPH_MCP_WRITE_SCOPE_NAME` scope (`access_as_user.write` by default) in the token's `scp`.

Expose that scope alongside `access_as_user` on the MCP's app registration. The header can only
*narrow* — a client that holds write authority may still decline to use it — but it grants nothing
on its own, because a header is something the caller sets for itself. A write tool refused for want
of the scope returns `403` with `WWW-Authenticate: Bearer error="insufficient_scope", scope="…"`,
which a conforming client uses to re-authorize and retry.

The header deciding alone is **deprecated, with removal in `0.5.0`**. Nothing changes in the
passthrough posture: there the token is audienced to Graph and its `scp` carries Graph permissions,
not scopes this server defines, so there is nothing to check against.

### The two auth postures

Selected by `GRAPH_MCP_DOES_OBO`:

- **Resource server (default).** The inbound token is audienced to *this server*. Audience binding
  is the gate, and the server exchanges that token for a Graph token via the on-behalf-of flow
  before the tool runs. This needs a tenant id, a client id and a client credential — **the server
  refuses to start without them**. Setting up the app registrations is covered in
  [agent-auth.md](agent-auth.md).
- **Passthrough** (`GRAPH_MCP_DOES_OBO=false`). The caller forwards an already-OBO'd Graph token,
  validated for the Graph audience *plus* `azp == our client_id`. **Deprecated since 0.4.0, removed
  in 1.0.0**, and it warns at startup.

Why the default changed: a token audienced to `https://graph.microsoft.com` was issued *for Graph*,
not for this server, and accepting one is the confused-deputy anti-pattern the MCP authorization
specification names. `azp` narrows who *minted* a token, never who it is for. Passthrough also
cannot satisfy a Conditional Access step-up, because the claims challenge has nowhere to go. See
[ADR 0004](adr/0004-resource-server-by-default.md).

> **Upgrading from 0.3.x?** A hosted deployment that never set `GRAPH_MCP_DOES_OBO` will not start
> until it has a credential. Either configure one — see [agent-auth.md](agent-auth.md) — or set
> `GRAPH_MCP_DOES_OBO=false` to keep the old behaviour while you migrate. stdio is unaffected.

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

See [SECURITY.md](https://github.com/nitin27may/ms-graph-mcp/blob/main/SECURITY.md) for what to change before exposing this beyond localhost.

---

## Toolset profiles

85 tools is a lot to put in front of a model. `GRAPH_MCP_TOOLSETS` selects named profiles, each a
group of namespaces. The table and the per-request `X-Toolsets` header are documented in the
[README](https://github.com/nitin27may/ms-graph-mcp/blob/main/README.md#toolset-profiles).

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
