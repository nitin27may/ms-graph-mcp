# Security Policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

Report it privately through GitHub's [private vulnerability
reporting](https://github.com/nitin27may/ms-graph-mcp/security/advisories/new), or by email to
nitin27may@gmail.com with `[ms-graph-mcp security]` in the subject.

Please include the version, a description of the impact, and steps to reproduce. You will get an
acknowledgement within 72 hours and an assessment within 7 days. This is a small project maintained
by one person — that is the honest commitment, not a corporate SLA.

## Supported versions

Pre-1.0. Only the latest release receives fixes. Once 1.0 ships, this table will list a support
window.

## What this server handles

`ms-graph-mcp` brokers a user's **delegated Microsoft Graph access token** to Microsoft Graph. A
vulnerability here can mean disclosure of, or unauthorised action on, someone's mail, calendar,
files, chats, and directory data. Treat findings in the following areas as high severity:

- **Token handling** — anything that logs, persists, or forwards an access token to a host other
  than Microsoft Graph.
- **Tier enforcement** — anything that lets a caller reach a write tool without write authorisation,
  or reach the internal tier without the machine principal.
- **Token validation** — anything that causes a token to be accepted with the wrong audience,
  issuer, `azp`, `scp`, or signature. **Audience binding is the primary gate** on the HTTP
  transport: a token issued for Microsoft Graph, or for any other service, must not be accepted
  here. It is validated on every request, including when signature verification is disabled.
- **Credential handling** — anything that exposes the confidential-client credential used for the
  on-behalf-of exchange, or that causes it to be used on behalf of the wrong caller.
- **OData / path injection** — anything that lets caller-supplied input escape a Graph path segment
  or `$filter` expression.
- **SSRF** — anything that causes a bearer-token-carrying request to reach a host other than
  `graph.microsoft.com`.

## Deployment guidance

The defaults favour a local, single-user stdio run. Several of them are wrong for anything exposed
beyond localhost:

| Setting | Default | For a network-reachable deployment |
|---|---|---|
| `GRAPH_MCP_JWT_VERIFY` | `true` | Leave it on. Turning it off stops token *signatures* being verified; it exists for local runs without JWKS connectivity. Audience is still enforced. |
| `GRAPH_MCP_DOES_OBO` | `true` | Leave it on. `false` accepts tokens audienced to Microsoft Graph rather than to this server — the token-passthrough pattern the MCP authorization specification forbids. Deprecated, removal in 1.0. |
| `GRAPH_MCP_CLIENT_SECRET` | `""` | Prefer `GRAPH_MCP_CLIENT_CERT_PATH` or `GRAPH_MCP_FEDERATED_TOKEN_FILE`. Microsoft's guidance is that secrets should not be used as client credentials in production; the server warns at startup if you use one. |
| `GRAPH_MCP_REQUIRED_SCOPE` | `""` (no gate) | Set it. Audience binding proves a token was issued for this server; it says nothing about what its bearer was granted, so without this any correctly-audienced token reaches the whole tool surface. |
| `GRAPH_MCP_WRITE_SCOPE_NAME` | `""` (header only) | Set it. Otherwise write access is decided by `X-Write-Scope`, a header the caller sets for itself, with Graph's own consent as the only backstop. |
| `GRAPH_MCP_ALLOWED_AZP` | `""` | Optional defence in depth. Pin the client ids permitted to call — for an Entra Agent ID, the *agent identity's* id, never the blueprint's. |
| `GRAPH_MCP_DISABLE_SSL_VERIFY` | `false` | Leave `false`. It exists for corporate TLS-inspection proxies and disables certificate validation on Graph calls. |
| `GRAPH_MCP_SHARED_SECRET` | `""` (no gate) | Set to a high-entropy value, or do not expose the HTTP transport. This secret unlocks the machine principal. |
| `GRAPH_MCP_SEND_EMAIL_ALLOWED_DOMAINS` | `""` (no gate) | Set to your tenant's domains if write tools are enabled, so an agent cannot mail arbitrary external recipients. |
| `GRAPH_MCP_READ_ONLY` | `false` | Set to `true` for any deployment that should never mutate tenant data. Removes the write tier entirely — stronger than relying on callers to omit the write scope. |

Two of these — `GRAPH_MCP_REQUIRED_SCOPE` and `GRAPH_MCP_WRITE_SCOPE_NAME` — default to *off* only
because the release that introduced them already changed the OBO posture, and two default changes at
once would make a `403` ambiguous to diagnose. They become non-empty in 0.5.0. Until then, setting
them is the difference between authorization that comes from the token and authorization that comes
from a header the caller chose.

**There is deliberately no way to skip authentication.** Token validation always runs in-server, so
a deployment that loses its gateway does not become an unauthenticated Graph proxy. See
[ADR 0003](docs/adr/0003-no-gateway-trust-mode.md); do not file this as a redundancy.

**The server does not relay tokens.** A caller's token is exchanged for a Graph token via the
on-behalf-of flow and never forwarded onward; the Graph token it obtains is never returned to the
caller. See [ADR 0004](docs/adr/0004-resource-server-by-default.md).

Additional expectations:

- **Terminate TLS in front of the server.** It speaks plain HTTP; it is meant to sit behind a
  reverse proxy or ingress.
- **The internal tier is not for agents.** It includes an arbitrary-path Graph passthrough
  (`graph_request`). It is unlocked only by the shared-secret machine principal plus an explicit
  header, and must never be exposed to an LLM-driven client.
- **Never put a token, client secret, or shared secret in a client config file that gets committed.**
  Use environment variables, your client's input-prompt mechanism, or a secret store.
- **Write tools are off by default.** Turning them on grants an LLM the ability to send mail and
  create content as the signed-in user.

## Scope

In scope: the code in this repository, its dependency manifest, and its release pipeline.

Out of scope: vulnerabilities in Microsoft Graph itself, in Microsoft Entra ID, or in MCP clients
(report those to their vendors); misconfiguration of your own Entra app registration; and findings
that require an attacker to already hold the user's valid Graph token.
