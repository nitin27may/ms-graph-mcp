# Hosting — Streamable HTTP

The stdio transport signs one user in and serves one client. The HTTP transport serves many callers,
each presenting their own token. This covers the second.

Settings are in [configuration.md](configuration.md#running-hosted-streamable-http--the-server-acts-for-many-users);
this is about running it.

```bash
# from a clone
GRAPH_MCP_PORT=8094 uv run --directory /path/to/ms-graph-mcp ms-graph-mcp-http

# or from the package
GRAPH_MCP_PORT=8094 uvx --from ms-graph-mcp ms-graph-mcp-http

curl -s localhost:8094/health
```

Two routes: `/mcp` for the protocol, and `/health`. `/health` is unauthenticated and does not touch
Graph, so a healthy response means the process is serving — not that Entra is reachable.

## Per-request headers

| Header | Purpose |
|---|---|
| `Authorization: Bearer <token>` | **Required.** Either a Microsoft Graph access token (validated as a real Entra JWT), or the configured shared secret for a machine caller. |
| `X-Write-Scope: true` | Expose *and* permit the write tools for this request. |
| `X-Toolsets: mail,calendar` | Narrow the advertised tool surface for this request. Can only narrow — the startup value is a ceiling. |
| `X-Entra-App-Token: <token>` | Optional app-only token for directory and group lookups that delegated permissions cannot cover tenant-wide. |
| `X-Internal-Scope: true` | Expose the internal deterministic tier. Honoured **only** for the shared-secret machine principal — never for a user token. |
| `X-OBO-Token: <token>` | Internal tier only: an explicitly supplied downstream token. |

The internal tier gates on the caller being a *machine principal*, which only the shared-secret
bypass sets. A real Entra client-credentials token does not qualify. That distinction came out of a
security audit and is asserted by tests in two places.

## Set `GRAPH_MCP_RESOURCE_URL` when you deploy behind a proxy

It does two things, and the second one will bite you if you skip it.

It turns on **OAuth discovery**: the server publishes RFC 9728 metadata at
`/.well-known/oauth-protected-resource/mcp` and answers an unauthenticated request with a `401`
carrying `WWW-Authenticate: Bearer resource_metadata="…"`. A spec-compliant MCP client follows that
pointer to find your tenant's authorization server on its own, rather than needing it configured by
hand. Left empty, discovery is simply off — the server cannot know its own public URL from behind a
proxy, and publishing a guess would send clients somewhere wrong.

It also **registers your hostname with the transport's DNS-rebinding protection**. The MCP SDK
validates the `Host` header and, by default, trusts only localhost. Since this process binds
`0.0.0.0`, a deployment behind an ingress receives requests with a real hostname — and without this
setting every one of them is refused with `421 Misdirected Request` before reaching any handler.
Localhost stays valid regardless, so local runs and MCP Inspector are unaffected.

```bash
GRAPH_MCP_RESOURCE_URL=https://graph-mcp.example.com/mcp
```

Use `GRAPH_MCP_ALLOWED_HOSTS` (comma-separated) only for *additional* names that URL does not cover
— a split-horizon DNS name, a service-mesh address, a second domain. Ports are wildcarded
automatically; the protection that matters is on the name, which is what a DNS-rebinding attack has
to control.

> **Getting `421 Misdirected Request` on every request?** That is this, and it is the most likely
> thing to go wrong on a first hosted deployment. Set `GRAPH_MCP_RESOURCE_URL` to the URL clients
> actually connect to.

**Dynamic client registration is not available.** Entra ID does not implement RFC 7591, so a client
cannot register itself from the discovery metadata alone. Clients need a pre-registered app id —
either yours, or their own with your API added as a permission. This is an Entra limitation, not
something this server can work around.

## Docker

The image serves the **HTTP transport only**. stdio speaks JSON-RPC over the process's own
stdin/stdout, so a client has to spawn it directly — wrapping that in `docker run` gains nothing and
breaks the interactive sign-in.

```bash
docker run --rm -p 8094:8094 \
  -e GRAPH_MCP_CLIENT_ID=<application-client-id> \
  -e GRAPH_MCP_TENANT_ID=<directory-tenant-id> \
  -e GRAPH_MCP_RESOURCE_URL=https://graph-mcp.example.com/mcp \
  ghcr.io/nitin27may/ms-graph-mcp:latest
```

Published to [GHCR](https://github.com/nitin27may/ms-graph-mcp/pkgs/container/ms-graph-mcp) on each
release for `linux/amd64` and `linux/arm64`, with build provenance attestations. Tags follow the
release: `latest` tracks the newest stable, plus `MAJOR.MINOR.PATCH` and `MAJOR.MINOR`. Pre-releases
are tagged with their full version (`0.3.0-rc1`) and never move `latest`.

Runs as a non-root user (uid 10001) that cannot write to its own virtualenv, and carries a
`HEALTHCHECK` against `/health`.

Build it yourself with `docker build -t ms-graph-mcp .`.

### Resource limits

Sensible for a single replica. The process is I/O-bound on Graph, not CPU-bound:

```yaml
resources:
  requests: { cpu: 50m,  memory: 128Mi }
  limits:   { cpu: 500m, memory: 512Mi }
```

## Embedding it instead

`build_app()` returns a Starlette app you can mount into a larger service — see
[configuration.md](configuration.md#embedding-in-your-own-app).

One trap: `streamable_http_app()` owns the app's lifespan, because it runs the session manager
there. If you need your own lifespan, chain onto `application.router.lifespan_context` rather than
replacing it — replacing it means the transport never starts.

## Before you expose it

[SECURITY.md](https://github.com/nitin27may/ms-graph-mcp/blob/main/SECURITY.md) is the checklist. The short version:

- `GRAPH_MCP_JWT_VERIFY` stays on. It defaults on for a reason.
- Set `GRAPH_MCP_RESOURCE_URL`, or nothing reaches a handler.
- Set `GRAPH_MCP_READ_ONLY=true` unless writes are genuinely needed — it removes the write tier from
  the deployment rather than trusting callers to omit a header.
- Set `GRAPH_MCP_SEND_EMAIL_ALLOWED_DOMAINS` if writes *are* enabled.
- Leave `GRAPH_MCP_SHARED_SECRET` empty unless you have a machine caller. It is what unlocks the
  internal tier.
