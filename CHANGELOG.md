# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0, the configuration surface may
change between minor versions; breaking changes are called out explicitly.

## [Unreleased]

## [0.2.0] - 2026-08-05

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
- **Toolset profiles.** `GRAPH_MCP_TOOLSETS` selects which namespaces are advertised, defaulting to
  `core` (23 read tools, ~4,200 tokens) rather than all 53 (~9,200). Over HTTP an `X-Toolsets`
  header may narrow further for a single request but can never widen beyond the startup value.
  Filters visibility only — the tier gates are untouched, and a hidden tool is still refused at
  dispatch if it was never permitted.
- **Interactive sign-in for the stdio transport.** Set `GRAPH_MCP_CLIENT_ID` and
  `GRAPH_MCP_TENANT_ID` and the server signs the user in through the browser — normal Microsoft 365
  SSO including MFA and conditional access — instead of requiring a pre-acquired token in a config
  file. Falls back to device code over SSH or in containers. Tokens are cached owner-only under
  `~/.ms-graph-mcp/`, and refreshed on every tool call so a session does not go stale after an hour.
  `GRAPH_MCP_ACCESS_TOKEN` still works for CI.
- `GRAPH_MCP_SCOPES` — the delegated scopes requested at sign-in. Read-only by default.
- README: app-registration walkthrough and copy-paste config for VS Code, Claude Code, Claude
  Desktop and MCP Inspector, plus a troubleshooting table for the common Entra errors.
- **Mail actions** (4 tools). `mail_reply`, `mail_reply_all`, `mail_forward`, `mail_mark_read`.
  Forwarding is subject to the recipient-domain allowlist because the caller chooses the
  recipients; replying is not, because the thread fixes them.
- **Teams chats** (4 tools). `chat_list`, `chat_list_messages`, `chat_send_message`,
  `chat_list_members` — the 1:1 and group conversations, where most Teams activity actually
  happens. Previously only channels were reachable.
- **Unified search** (1 tool). `search_query` over `POST /search/query`, spanning mail, calendar,
  files, SharePoint sites and lists, and people in a single call.
- **OneNote page reads** (2 tools). `notes_list_pages` and `notes_get_page_content` — the server
  could write a page but not read one back.
- **Contacts** (3 tools). `people_list_contacts`, `people_search_contacts`,
  `people_create_contact`. The personal address book is the only place external contacts live,
  invisible to both `people_search` and `directory_search_users`.
- **Task writes for To Do and Planner** (5 tools). `tasks_complete_todo`, `tasks_update_todo`,
  `tasks_create_planner`, `tasks_update_planner`, `tasks_complete_planner`. Planner writes handle
  the ETag it requires on every change: each is read-then-write, and a concurrent edit comes back
  as a retryable `CONFLICT` rather than silently overwriting someone.
- **Calendar write and scheduling** (6 tools). `calendar_create_event`, `calendar_update_event`,
  `calendar_cancel_event` and `calendar_respond_to_event` in the write tier;
  `calendar_find_meeting_times` and `calendar_get_free_busy` in the **read** tier — both are POST
  because their request body is too large for a query string, but neither mutates anything.
  Booking was the largest functional gap in the surface.
- `GRAPH_MCP_READ_ONLY` — removes the write tier from a deployment entirely. Write tools are never
  advertised and never dispatchable, whatever scope a caller presents. Enforced at dispatch as well
  as in `tools/list`, because hiding a tool is a context measure rather than a boundary.
- [ADR 0003](docs/adr/0003-no-gateway-trust-mode.md) recording that token validation always runs
  in-server and that no gateway-trust bypass will be added. A test asserts no such setting exists.
- **OAuth protected-resource discovery (RFC 9728).** With `GRAPH_MCP_RESOURCE_URL` set, the server
  publishes `/.well-known/oauth-protected-resource` and answers an unauthenticated request with a
  `401` carrying `WWW-Authenticate: Bearer resource_metadata="…"`, so a spec-compliant MCP client
  can discover the tenant's authorization server rather than needing it configured by hand. Unset,
  discovery is off — behind a proxy the server cannot know its own public URL, and publishing a
  guess would send clients somewhere wrong. Entra implements no RFC 7591 dynamic client
  registration, so clients still need a pre-registered app id; that limitation is documented.
- `GRAPH_MCP_ALLOWED_HOSTS` — additional `Host` values the HTTP transport accepts, for names the
  resource URL does not cover.
- `GRAPH_MCP_LOG_LEVEL` — applies to both console entry points, defaulting to `WARNING` so an
  ordinary run stays quiet. Everything is written to stderr at every level, including on stdio
  where stdout carries JSON-RPC. Previously nothing configured logging at all, so the per-request
  `[Graph]` lines in `client.py` could not be turned on.
- **Container image**, published to GHCR on release for `linux/amd64` and `linux/arm64` with build
  provenance attestations. Multi-stage, non-root (uid 10001, unable to write to its own virtualenv),
  with a `HEALTHCHECK` against `/health`. HTTP transport only — stdio speaks JSON-RPC over the
  process's own stdin/stdout and gains nothing from a container.
- `docs/testing.md` and `docs/debugging.md` — running the suite, driving the server with MCP
  Inspector, and reading the logs, error codes and Entra failures.

### Changed

- **BREAKING: `GRAPH_MCP_JWT_VERIFY` now defaults to `true`.** It previously defaulted to `false`,
  which meant an HTTP deployment accepted tokens without verifying their signatures unless someone
  had read the docs — the unsafe value was the one you got by doing nothing. stdio is unaffected, as
  it validates no tokens at all. If you run the HTTP transport without JWKS connectivity and
  knowingly want the old behaviour, set `GRAPH_MCP_JWT_VERIFY=false` explicitly.
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
- References to the monorepo this package was extracted from are gone. The self-containment test
  no longer checks a hardcoded list of former sibling packages; it now derives the allowed import
  set from `pyproject.toml`, so an undeclared dependency fails the build here rather than on a
  user's machine.
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
- The release pipeline is staged rather than two parallel tracks. Every release now goes
  `build → testpypi → verify → pypi`, where `verify` installs the artifact back out of TestPyPI
  into a clean environment and runs it. Previously a real tag went straight to PyPI having never
  touched TestPyPI.

- Release candidates are published to PyPI as well as TestPyPI, once a stable release exists —
  `pip install --pre ms-graph-mcp`, or an exact pin. pip skips pre-releases otherwise, so a
  candidate never reaches anyone who did not ask for one. A `gate` step blocks a candidate from
  reaching PyPI *before* the first stable release, where being the only version on the index would
  make a plain `pip install` resolve to it.

- CI runs an **MCP Inspector** smoke check — `tools/list` and a `tools/call` through the reference
  client, asserting every tool carries annotations, that no write tool is advertised without write
  scope, and that a call without credentials fails closed. Inspector is not built on the SDK this
  server uses, so it sees what a real client would; the pytest suite drives the server with the same
  library it is implemented with and cannot catch an SDK-level bug.

### Fixed

- **The HTTP transport rejected its own public hostname.** The MCP SDK keys DNS-rebinding
  protection off the `host` argument to `streamable_http_app()`, and the default `127.0.0.1` makes
  it trust localhost alone. This process binds `0.0.0.0`, so behind an ingress every request
  arrived with a real hostname and was refused with `421 Misdirected Request` before reaching any
  handler. Accepted hosts are now explicit, derived from `GRAPH_MCP_RESOURCE_URL`. Localhost stays
  valid and an unrelated host is still refused.
- The release workflow stripped `-rc` before comparing the tag to `pyproject.toml`, so tagging
  `v0.2.0-rc1` against version `0.2.0` passed and would have published a *stable* `0.2.0` from a
  release-candidate tag. Tag and version are now compared as PEP 440 versions.
- `missing_graph_token` told stdio users to set the `X-Graph-Token` header, which does not exist in
  that transport. It now names the fix for the transport in use.
- MCP Inspector does not pass its own environment to the server it spawns, so the documented
  `GRAPH_MCP_CLIENT_ID=… npx @modelcontextprotocol/inspector …` invocation started the server with
  no client id at all. The README now uses `-e`.
- README links are absolute, so they resolve on the PyPI project page, which renders the file with
  no repository to resolve relative paths against.

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

[Unreleased]: https://github.com/nitin27may/ms-graph-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/nitin27may/ms-graph-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/nitin27may/ms-graph-mcp/releases/tag/v0.1.0
