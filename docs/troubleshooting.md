# Troubleshooting

Setup failures, in rough order of how often they are the answer. For reading logs and error codes
once the server is running, see [debugging.md](debugging.md).

## The fast triage

| Symptom | Cause |
|---|---|
| `AADSTS7000218` on sign-in | **Allow public client flows** is not enabled on the app registration. |
| `AADSTS50011` redirect mismatch | The redirect URI is not `http://localhost`, or the platform is not *Public client/native*. |
| `SCOPE_DENIED` from a tool | The permission that tool needs was not consented. The error names it — add it in **API permissions** and sign in again. |
| Sign-in prompts every time | The cache at `~/.ms-graph-mcp/token_cache.json` is not writable. |
| Browser never opens | Expected over SSH or in containers — use the device code printed to stderr. |
| `AADSTS53003`, or "You cannot access this right now" **after** a successful sign-in | A Conditional Access policy requires a registered or compliant device. [See below](#aadsts53003--blocked-by-conditional-access). |
| Client shows "server disconnected" | Run the same command in a terminal; startup errors go to stderr and the client usually hides them. |
| Tools are missing from the list | `GRAPH_MCP_TOOLSETS` defaults to `core`. Teams chat, Planner, OneNote, transcripts and directory are not in it — name those profiles, or set `all`. |
| Write tools are missing | They need `GRAPH_MCP_WRITE_SCOPE=true` *and* the matching scopes, and are absent entirely if `GRAPH_MCP_READ_ONLY` is set. |
| `421 Misdirected Request` from a hosted deployment | `GRAPH_MCP_RESOURCE_URL` is not set, so the transport trusts only localhost. See [hosting.md](hosting.md#set-graph_mcp_resource_url-when-you-deploy-behind-a-proxy). |
| `[SSL: CERTIFICATE_VERIFY_FAILED]` when calling Graph tools | A TLS-inspecting proxy is re-signing Graph traffic with a chain your Python/OpenSSL runtime does not trust. [See below](#ssl-certificate-verify-failures-behind-corporate-proxies). |

## First: run the server in a terminal

MCP clients start the server as a subprocess and usually hide its stderr, so a startup failure
surfaces as nothing more than "server disconnected". Running the same command yourself is the single
highest-yield step:

```bash
GRAPH_MCP_CLIENT_ID=… GRAPH_MCP_TENANT_ID=… uvx --from ms-graph-mcp ms-graph-mcp
```

A healthy stdio server prints nothing and waits — it is speaking JSON-RPC on stdin/stdout. Press
Ctrl-C. Anything printed is either a diagnostic on stderr or a bug.

## Two things that catch people out with local clones

**`uv` must be on the client's PATH.** GUI apps launched from Finder or the Dock do not inherit your
shell's PATH, so a client can fail to start the server with an unhelpful error. If that happens, use
the absolute path:

```bash
which uv     # e.g. /Users/you/.local/bin/uv
```

and put that in `"command"` instead of `"uv"`.

**`--directory` is not optional.** Without it, `uv run` resolves against whatever directory the
client happened to launch from, which will not be the project.

## AADSTS53003 — blocked by Conditional Access

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

## SSL certificate verify failures behind corporate proxies

When this server calls Microsoft Graph, `httpx` validates the TLS chain using the Python/OpenSSL
trust store of the process running `ms-graph-mcp`.

In corporate networks with TLS inspection, Graph certificates are often re-issued by a proxy CA. If
that CA chain is not accepted by your runtime, Graph calls fail with errors like:

```text
[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate chain
```

or

```text
[SSL: CERTIFICATE_VERIFY_FAILED] Basic Constraints of CA cert not marked critical
```

Use this workaround only when you have confirmed proxy-related TLS interception and cannot quickly
repair trust-chain validation:

```jsonc
"GRAPH_MCP_DISABLE_SSL_VERIFY": "true"
```

Where to set it:

- VS Code workspace config (`.vscode/mcp.json`) under the server `env` block.
- User-level MCP config for clients that support per-server environment variables.

**Security trade-off.** This disables certificate validation for Graph HTTP calls from this server
process. It reduces protection against machine-in-the-middle attacks and should be treated as a
temporary compatibility escape hatch, not a default posture.

Preferred long-term fix:

1. Install and trust the corporate proxy CA chain in the runtime trust store used by Python/OpenSSL.
2. Remove `GRAPH_MCP_DISABLE_SSL_VERIFY` (or set it back to `false`).
3. Restart the MCP client/server process and verify Graph calls succeed with TLS verification on.

## Still stuck

- [Discussions](https://github.com/nitin27may/ms-graph-mcp/discussions) for setup and
  app-registration questions.
- [Issues](https://github.com/nitin27may/ms-graph-mcp/issues) for a bug, with the output of the
  terminal run above. **Redact the `Authorization` header** from anything you attach.
- Never paste an access token, client secret or shared secret into either.
