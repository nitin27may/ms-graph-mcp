# ADR 0003 — Token validation is always performed in-server

- **Status:** Accepted
- **Date:** 2026-08-05

## Context

This server is commonly deployed behind an API gateway — Azure API Management, an ingress
controller, or similar — which already validates the bearer token before forwarding the request.
Validating it a second time looks like pure duplication, and the obvious optimisation is a setting
along the lines of:

```
GRAPH_MCP_AUTH_MODE=gateway   # trust the caller, the gateway already checked
```

That setting will not be added.

## Decision

Token validation always runs in-server. There is no configuration that disables it, and none will be
accepted.

## Consequences

**The failure mode is what decides this.** A flag whose meaning is "something else is checking"
depends on a fact the server cannot verify. Eventually someone deploys with the flag set and without
the gateway in front — a new environment, a copied Helm values file, a migration, a local
reproduction that gets promoted. The result is not a subtle degradation: it is an unauthenticated
proxy to Microsoft Graph that will act on any token it is handed.

Config validation cannot prevent this, because from the server's perspective the misconfiguration is
indistinguishable from the intended configuration.

With validation always on:

- A gateway becomes **purely additive** — rate limiting, WAF, request logging, IP restrictions. It
  passes `Authorization` through untouched and the server behaves identically with or without it.
- There is **one code path** in both deployment shapes, so the authenticated path is the one
  exercised by every test and every local run.
- Nothing can be misconfigured into a bypass, because no bypass exists.

**The cost is real but small.** Every request pays a JWKS-cached signature verification. The JWKS
document is fetched once and cached per issuer, so the steady-state cost is an in-process RSA verify
— microseconds against a Graph call measured in tens of milliseconds.

**This is not the same as `GRAPH_MCP_JWT_VERIFY`.** That setting controls whether the *signature* is
checked and now defaults to `true` (see CHANGELOG for 0.2.0). Turning it off is a deliberate,
auditable act for a specific local situation — usually no JWKS connectivity — and it still extracts
and applies the principal. It is not a "trust the network" mode, and the distinction matters: one is
a narrowing of a check, the other would be an abandonment of the whole seam.

**The shared-secret machine principal is also not an exception.** It is an explicit credential the
caller must present and that the operator must configure; an empty `GRAPH_MCP_SHARED_SECRET` cannot
match anything. It authenticates a caller rather than skipping authentication.

## Enforcement

`tests/test_security_defaults.py::TestNoGatewayTrustMode` asserts that no setting named
`auth_mode`, `skip_auth`, `trust_gateway`, `disable_auth` or `allow_anonymous` exists on the config
model. Adding one fails the build, which forces whoever adds it to read this ADR first.

## References

- `src/ms_graph_mcp/auth.py` — the middleware, applied to every path except `/health`
- `src/ms_graph_mcp/entra/middleware.py` — `authenticate_request`, the shared auth chain
- MCP authorization specification — servers act as OAuth 2.1 resource servers and **must** validate
  that tokens were issued for them
