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

- Every tool now declares MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
  `openWorldHint`) so clients can tell a calendar read from a mail send.
- `errors.py` — terminal structured errors carrying `retryable`, plus the required scope on a
  permission denial, so a model stops retrying a call that can never succeed.
- `client.py` gained `graph_post_no_content`, `graph_post_raw`, `graph_put_raw` and
  `graph_try_get`, covering the request shapes that previously forced modules to hand-roll httpx.

### Changed

- **All 51 agent-facing tools renamed to namespace-prefixed names** — `mail_search`,
  `calendar_list_upcoming_events`, `files_upload`, `directory_search_users` and so on. Namespaces
  follow Graph permission families rather than Microsoft product names. **Every previous name still
  works as an alias** and will keep working until 0.3.0; aliases are accepted by `tools/call` but
  never advertised in `tools/list`.
- Every tool description rewritten to 200–400 characters covering what it does, when to use it,
  what it returns, how it differs from neighbouring tools, and the delegated permission required.
  Previously 83% were under 200 characters, with the shortest at 33 — too terse for a model to
  choose between `people_search`, `directory_search_users` and `people_list_contacts`.
- `chat_search_messages` no longer disguises failures as empty results. It previously returned `[]`
  for any non-200, so a missing `Chat.Read` permission was indistinguishable from "no messages
  matched".
- Raw `httpx` clients removed from every domain module — 14 in `meetings.py` alone. The two that
  remain target pre-signed upload/download URLs on other hosts, which are not Graph API calls and
  must not carry the Authorization header.
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
