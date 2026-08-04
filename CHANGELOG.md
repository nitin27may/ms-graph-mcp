# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0, the configuration surface may
change between minor versions; breaking changes are called out explicitly.

## [Unreleased]

### Added

- `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and this changelog.
- Architecture Decision Records under `docs/adr/` — the `src/` layout and the raw-`httpx` Graph
  client.
- GitHub Actions CI: ruff lint and format checks, pytest on Python 3.12 and 3.13, and a build job
  that installs the built wheel into a clean environment and resolves the tool allowlists from it.
- `CLAUDE.md` documenting the architecture, the add-a-tool checklist, and the security invariants.

### Changed

- Applied `ruff format` across the codebase. No behaviour change.

## [0.1.0]

Initial extraction from the agent platform this server was built for.

### Added

- 55 Microsoft Graph tools across calendar, email, meetings, Teams, files, people, directory, tasks,
  and OneNote, split into three enforced tiers: 42 read, 4 write, 9 internal.
- Two transports: stdio for local MCP clients, and Streamable HTTP.
- Read/write separation enforced at dispatch — write tools are hidden and refused unless the caller
  explicitly opts in.
- An internal deterministic tier, reachable only by the shared-secret machine principal, for
  non-agent callers.
- A self-contained Entra ID token-validation toolkit (`ms_graph_mcp.entra`): JWKS-backed RS256
  verification, issuer/audience/expiry checks, `azp` allowlisting, and a machine bypass.
- On-behalf-of and client-credentials token exchange for resource-server deployments.
- A raw `httpx` Graph client with OData-safe URL construction, OTEL tracing spans, 429 retry with
  `Retry-After`, and a host allowlist on caller-supplied URLs.
- Meeting transcript and attendance-report retrieval.

[Unreleased]: https://github.com/nitin27may/ms-graph-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nitin27may/ms-graph-mcp/releases/tag/v0.1.0
