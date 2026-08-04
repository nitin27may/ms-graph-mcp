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
  issuer, `azp`, or signature.
- **OData / path injection** — anything that lets caller-supplied input escape a Graph path segment
  or `$filter` expression.
- **SSRF** — anything that causes a bearer-token-carrying request to reach a host other than
  `graph.microsoft.com`.

## Deployment guidance

The defaults favour a local, single-user stdio run. Several of them are wrong for anything exposed
beyond localhost:

| Setting | Default | For a network-reachable deployment |
|---|---|---|
| `GRAPH_MCP_JWT_VERIFY` | `false` | **Set to `true`.** Off by default only so a local run works without JWKS connectivity. Leaving it off means token signatures are not verified. |
| `GRAPH_MCP_DISABLE_SSL_VERIFY` | `false` | Leave `false`. It exists for corporate TLS-inspection proxies and disables certificate validation on Graph calls. |
| `GRAPH_MCP_SHARED_SECRET` | `""` (no gate) | Set to a high-entropy value, or do not expose the HTTP transport. This secret unlocks the machine principal. |
| `GRAPH_MCP_SEND_EMAIL_ALLOWED_DOMAINS` | `""` (no gate) | Set to your tenant's domains if write tools are enabled, so an agent cannot mail arbitrary external recipients. |

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
