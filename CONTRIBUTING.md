# Contributing to ms-graph-mcp

Thanks for taking the time. This project is a Microsoft Graph MCP server with a deliberately small
dependency tree and a security model that is enforced in code rather than described in docs. Most of
this guide is about the second part.

## Getting set up

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/nitin27may/ms-graph-mcp
cd ms-graph-mcp
uv sync
uv run pytest -q
```

## The verification chain

Run all three before opening a pull request. CI runs the same commands, so a green local run means a
green PR.

```bash
uv run ruff check .          # lint (auto-fixes locally; CI uses --no-fix)
uv run ruff format .         # format
uv run pytest -q             # 548 tests and counting
```

Useful subsets:

```bash
uv run pytest tests/test_meetings.py -q     # one file
uv run pytest -k "write_scope" -q           # one concern
```

`asyncio_mode = "auto"` is set, so async tests need no `@pytest.mark.asyncio`.

## Adding a Graph tool

Four steps. Steps 3 and 4 are not optional — skipping them breaks the server or the suite.

1. **Write the tool** in the relevant domain module (`calendar.py`, `email.py`, …). It must be an
   `async def` taking `(params: SomePydanticModel, context: dict)` and decorated with
   `@tool(description=...)`. A sync function raises `TypeError` at decoration time rather than
   failing later inside a tool-calling loop.

2. **Take the token from `context["access_token"]`.** The one exception is directory *group*
   lookups, which prefer `context.get("entra_app_token")` with a fallback, because delegated
   permissions cannot cover tenant-wide group reads.

3. **Add the name to exactly one tuple in `allowlists.py`** — `READ_TOOL_NAMES`,
   `WRITE_TOOL_NAMES`, or `INTERNAL_TOOL_NAMES`. A registered tool that is in no allowlist is
   unreachable. An allowlist entry with no registered tool raises `RuntimeError` on the next
   `tools/list` — the server refuses to serve a partial surface rather than silently dropping a
   tool.

4. **If it is a read tool, bump the count** in `tests/test_allowlists.py`. That assertion exists so
   that changing the tool surface is a deliberate edit rather than an accident.

Anything caller-supplied that ends up in a Graph path or an OData `$filter` must go through the
helpers in `odata.py` — `validate_graph_id`, `validate_mail_folder`, `validate_task_status`,
`escape_odata_string`. Do not hand-roll the escaping.

## Invariants that are not up for negotiation

These are enforced by tests. If one is in your way, the answer is a design discussion in an issue,
not a workaround.

- **A write or internal tool must never appear in the read allowlist.**
  `assert_no_write_in_reads()` fails loudly on a leak. Internal tools must never appear in
  `READ_TOOL_NAMES` or `WRITE_TOOL_NAMES` at all — that exclusion is what keeps them off the
  agent-visible `tools/list`.
- **The internal tier gates on `principal.is_machine`, never `is_app_only`.** A real Entra
  client-credentials token also sets `is_app_only`; only the shared-secret bypass sets `is_machine`.
  This distinction came out of a security audit.
- **Dispatch fails closed.** Unknown tool name, missing scope, and missing Graph token each return a
  structured error before any call to Graph is made.
- **The package must not import its original monorepo.** `test_package_is_self_contained` AST-walks
  every module and fails on imports of `shared`, `agents`, `integrations`, `control_plane`,
  `wg_tool_core`, or `wg_service_auth`.
- **`msgraph-sdk` and `azure-identity` are not dependencies.** See
  [ADR 0002](docs/adr/0002-raw-httpx-graph-client.md) before proposing them.

`CLAUDE.md` documents the same invariants alongside the traps in `client.py` — worth reading before
your first change.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `test:`,
`refactor:`, `style:`, `chore:`. Scope where it helps — `feat(calendar):`.

Commits carry no tool attribution — no `Co-Authored-By` for AI assistants, no generated-by footers.
Write the subject and body, then stop.

## Pull requests

- One concern per PR. A formatting sweep and a behaviour change in the same diff is two PRs.
- New tools need tests against a mocked Graph, in the style of the existing domain tests.
- Update `CHANGELOG.md` under `## [Unreleased]`.
- Note any new environment variable in `README.md`.

## Reporting security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).

## Releasing

Maintainers only.

1. Bump `version` in `pyproject.toml`.
2. Move `## [Unreleased]` entries into a new version heading in `CHANGELOG.md`.
3. Merge to `main`.
4. Tag a release candidate — `git tag v0.2.0-rc1 && git push --tags` — which publishes to TestPyPI.
5. Install from TestPyPI on a clean machine and verify.
6. Tag the real release — `git tag v0.2.0 && git push --tags`.
7. Approve the `pypi` environment in the Actions run. The publish job uses PyPI Trusted Publishing
   (OIDC); there is no API token to manage.
8. Verify `uvx --from ms-graph-mcp ms-graph-mcp` from a clean machine.
