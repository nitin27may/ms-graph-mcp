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

- `tests/test_protocol_conformance.py` — drives a real `mcp.Client` session against the server
  in-process, covering protocol negotiation, tool advertisement, scope gating, and error shapes.
- `serverInfo` now reports the installed package version, a title, and the project URL. It
  previously sent an empty version string.
- Cache hints on `tools/list` (`ttlMs` 5 minutes, `cacheScope: public`), which improve client-side
  prompt-cache hit rates on a tool list that only changes with the caller's scopes.

### Changed

- **Migrated to MCP Python SDK 2.0** and the 2026-07-28 protocol revision. Handlers are now
  constructor callbacks (`on_list_tools` / `on_call_tool`) returning `ListToolsResult` /
  `CallToolResult`, and the low-level `Server` builds the Starlette app itself. 2025-era clients are
  still served from the same server.
- Tool failures now set `isError: true` on the result instead of returning a plain JSON blob, so
  clients feed them back to the model for self-correction. Structured `invalid_arguments` responses
  from the tool registry are marked the same way.
- Dependency floors raised to match MCP 2.0: `pydantic>=2.12`, `starlette>=1.0`,
  `pyjwt[crypto]>=2.10.1`. `httpx2` arrives transitively; the Graph client stays on `httpx`.
- The Streamable HTTP request body limit is set to 16 MiB, up from the SDK's 4 MiB default, so the
  internal tier's base64 upload tool is not capped at roughly 3 MiB of actual file.
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
