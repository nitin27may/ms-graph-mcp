# Glossary

Terms these docs use, defined as they apply **here** rather than in general. Microsoft and OAuth
both overload several of them, and the differences are where the confusion lives.

## Identity and tokens

**Delegated permission** (`scp` claim) — a permission the *user* granted an application to act on
their behalf. `Mail.Read` as a delegated permission means "read the signed-in user's mail". Every
tool in this server uses delegated permissions; it can never see more than the user can.

**Application permission** (`roles` claim) — a permission granted to an *application* itself, with
no user involved. `Mail.Read` as an application permission means "read anyone's mail". This server
deliberately uses none, which is why entire Graph workloads are
[out of scope](graph-coverage.md#out-of-scope--and-why).

**`aud` (audience)** — who a token was issued *for*. The single most important claim here: a token
audienced to `https://graph.microsoft.com` was issued for Graph, and this server must reject it. The
MCP authorization specification requires a server to accept only tokens issued for itself.

**`azp` / `appid` (authorized party)** — which client application obtained the token. Says who
*minted* a token, not who it is *for*. Useful as defence in depth
(`GRAPH_MCP_ALLOWED_AZP`), useless as a substitute for audience — which is the mistake the
deprecated passthrough posture makes.

**`scp` (scope)** — the delegated permissions present in a token, space-delimited. Audience proves a
token was issued for this server; `scp` proves what its bearer was granted. Both are needed:
without the second, any correctly-audienced token reaches the whole tool surface.

**Claims challenge** — a blob Entra returns when a policy needs satisfying (MFA, sign-in frequency,
a compliant device). The server must hand it back in a `401`; the client acquires a *new* token
presenting it. Retrying the old token fails identically, which is why swallowing the challenge makes
step-up impossible.

**ID token vs access token** — an ID token says who signed in and is for the client that requested
it. Only access tokens may be presented to a service, so this server rejects anything carrying a
`nonce`.

## Flows

**OBO (on-behalf-of)** — a service exchanges a token it received for a *different* token, keeping
the same user's identity. The middle tier presents its own credential plus the caller's token, and
gets back a token for the downstream API. This is how a caller's MCP-audienced token becomes a Graph
token. Works only for user principals — an app-only token cannot be exchanged.

**Client credentials** — a service authenticating as *itself*, no user involved. Used here only by
the internal tier's app-only probe.

**PKCE** — how a public client proves it started the flow it is finishing, without a secret. The
stdio transport signs you in this way.

**RFC 9728 (Protected Resource Metadata)** — the document at
`/.well-known/oauth-protected-resource` that tells a client which authorization server to use.
Published when `GRAPH_MCP_RESOURCE_URL` is set, so a spec-compliant client can discover how to
authenticate instead of being configured by hand.

**RFC 8707 (Resource Indicators)** — how a client asks for a token audienced to a *specific*
resource. What makes audience binding possible on the client side.

## Application shapes

**Public client** — an app that cannot keep a secret, because it runs on the user's machine where
anyone with the config file or the process can read it. The stdio setup registers this way, with
PKCE instead of a secret. **A public client cannot be an OBO middle tier.**

**Confidential client** — an app running somewhere you control, which *can* hold a credential. The
hosted HTTP transport is one. This is why testing the agent flow generally wants a second app
registration rather than reusing the local one.

**Client credential** — what a confidential client authenticates with. In precedence order here:

| | |
|---|---|
| **Certificate** | A PEM bundle. The production default. |
| **FIC** (federated identity credential) | Trades a platform-issued token — an AKS workload identity, a managed identity — for an Entra token. No secret at rest. |
| **Client secret** | A password. Works, warns at startup; Microsoft's guidance is not to use one in production. |

## Entra Agent ID

**Agent identity blueprint** — the parent registration describing an agent. Holds the credential and
the delegated permissions.

**Agent identity** — a child of the blueprint, one per running agent. It performs the OBO exchange,
and **its** client id is what appears in `azp` on the token this server receives — never the
blueprint's. That matters if you pin callers with `GRAPH_MCP_ALLOWED_AZP`.

**`fmi_path`** — the parameter naming which child identity a blueprint is acting as during a token
exchange.

**`InheritDelegatedPermissions`** — lets agent identities inherit the blueprint's delegated
permissions, so running many instances does not mean consenting many times.

## This project

**Posture** — which auth model the HTTP transport is in, selected by `GRAPH_MCP_DOES_OBO`. Resource
server (default) or passthrough (deprecated). Has no meaning for stdio. See
[ADR 0004](adr/0004-resource-server-by-default.md).

**Tier** — read, write or internal. Read is always exposed; write needs authorization; internal is
reachable only by the machine principal. Enforced at dispatch, not just in `tools/list`.

**Machine principal** — a caller that presented `GRAPH_MCP_SHARED_SECRET` rather than a user token.
The *only* thing that unlocks the internal tier. Distinct from an app-only token, which does not
qualify — a distinction that came out of a security audit.

**Toolset profile** — a named group of namespaces controlling which tools are *advertised*.
Visibility, not authority: a hidden tool is still refused by the tier gates.

**Namespace** — the prefix on every tool name (`mail_`, `files_`, …), tracking Graph permission
families rather than Microsoft product names. `files_` covers OneDrive *and* SharePoint document
libraries because both are the same `driveItem` underneath.

## See also

- [agent-auth.md](agent-auth.md) — how these fit together in the on-behalf-of chain
- [configuration.md](configuration.md) — the settings each of them maps to
- [permissions.md](permissions.md) — every tool and the delegated permission it needs
