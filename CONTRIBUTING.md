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
uv run pytest -q             # full suite
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

5. **Name the delegated permission in the description** — `… Requires Mail.Read.` — and regenerate
   the derived documentation:

   ```bash
   uv run python scripts/generate_permissions.py   # docs/permissions.md, from the descriptions
   uv run python scripts/check_docs.py             # tool counts quoted in prose
   ```

   `docs/permissions.md` is generated from those descriptions. The counts quoted in `README.md`,
   `CLAUDE.md` and `docs/` are derived from the allowlists. CI runs both with `--check`, so a stale
   copy fails the PR rather than reaching a reader.

   `check_docs.py` also fails when one of its patterns matches *nothing*. Rewording a sentence it
   anchors on would otherwise leave that number silently unchecked from then on — the same drift,
   just quieter.

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
- **Every third-party import must be declared in `pyproject.toml`.**
  `test_package_imports_nothing_undeclared` derives the allowed set from the dependency list, so
  adding a dependency updates the test automatically and forgetting to declare one fails it.
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
- Note any new environment variable in **both** `docs/configuration.md` and `.env.example`. The
  README carries only the handful needed to get running.

## Reporting security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).

## Deprecation policy

Anything a user's configuration or code can name is covered: tool names, environment variables,
header names, error codes, and defaults.

**Nothing is ever removed in a patch release.** Someone taking `0.2.0` → `0.2.1` for a bug fix must
not find working code broken underneath them.

**A deprecation lasts at least one release cycle.** Pre-1.0 that means one minor version — deprecate
in `0.2.0`, remove no earlier than `0.3.0`. Post-1.0, removals wait for the next major. Anyone who
upgrades every other release should never be surprised.

While deprecated, the old thing **keeps working**:

- Renamed tools register the old name via `aliases=("old_name",)`. `tools/list` advertises only the
  canonical name — so deprecated names cost no context — while `tools/call` still honours the old
  one and logs a deprecation warning.
- The CHANGELOG gets a `Deprecated` section naming the replacement and the removal version.
- The entry goes in [`src/ms_graph_mcp/deprecations.py`](src/ms_graph_mcp/deprecations.py).

That last step is what makes the promise real. `tests/test_deprecations.py` **fails the build once
the package version reaches a `remove_in`**, comparing on the release tuple so entering the `0.4.0`
cycle — `0.4.0rc1` — already counts as due. Removal then becomes a deliberate edit rather than
something dependent on memory, and pushing a date back is allowed as long as somebody decides to.

There are **3 registered deprecations**. Read
[`deprecations.py`](src/ms_graph_mcp/deprecations.py) for the current list rather than a copy of it
here — each entry carries its own reasoning, and a restatement in prose is one more thing to forget.
The two that will affect a deployment rather than a caller:

- The token-passthrough posture (`GRAPH_MCP_DOES_OBO=false`), removal in `1.0.0`.
- `GRAPH_MCP_REQUIRED_SCOPE` / `GRAPH_MCP_WRITE_SCOPE_NAME` defaulting to empty, changing in
  `0.5.0`.

## Releasing

Maintainers only.

### Preparing the version

Use **Actions → Prepare release → Run workflow** with the new version. It bumps `pyproject.toml`,
rolls `## [Unreleased]` into a dated section, updates the changelog link definitions, runs the full
suite on the bumped tree, and opens a pull request.

It deliberately opens a PR rather than pushing to `main`. The diff is two files, and the version
number is the one part of a release that cannot be corrected afterwards — PyPI will not let a
version be reused, even after a yank.

A prerelease bumps the version but leaves `## [Unreleased]` alone: a candidate is a rehearsal, and
cutting a dated section for it would strand the entries under a version nobody installs.

The script runs standalone too:

```bash
uv run python scripts/prepare_release.py 0.3.0
```

Every release — candidate or real — takes the same path, and PyPI is only ever reached from the far
end of it:

```
build ─→ testpypi ─→ verify ─┬─→ pypi (real tags only, needs approval)
                             ├─→ ghcr
                             └─→ github-release
```

**Candidates are published to PyPI as well**, once a stable release exists. pip skips pre-releases
unless the user passes `--pre` or pins the version exactly, so a candidate is invisible to ordinary
installs while still being available to anyone who wants it — which beats sending people to
TestPyPI, a sandbox for the publishing process rather than a distribution channel.

The exception, enforced by the `gate` step in `build`: that protection only holds once a stable
release exists. A pre-release published as the *only* version on the index **is** installed by a
plain `pip install`, so a candidate cannot reach PyPI until a stable version is there to shadow it.
The step checks the live index rather than trusting the tag.

`verify` installs the artifact back out of TestPyPI into a clean venv, imports it, and speaks
`initialize` to the console script. A package that builds but does not install — a module missing
from the wheel, a dependency that will not resolve, a broken entry point — fails there, before the
version number is spent. **A PyPI version cannot be reused**, even after a yank, so the rehearsal is
automatic rather than something to remember.

1. Run **Prepare release** with the candidate version and merge the PR it opens.
2. Tag a candidate — `git tag v0.2.0-rc1 && git push --tags`. For this, `pyproject.toml` must say
   `0.2.0rc1`: the tag and the version are compared as PEP 440 versions, so `v0.2.0-rc1` and
   `0.2.0rc1` match, but `v0.2.0-rc1` against a plain `0.2.0` is rejected rather than quietly
   publishing a stable release from a candidate tag.
3. The run stops after `verify`. Read its log — that is the rehearsal.
4. Run **Prepare release** again with the stable version, merge, then
   `git tag v0.2.0 && git push --tags`.
5. Approve the `pypi` environment in the Actions run.
6. Verify `uvx --from ms-graph-mcp ms-graph-mcp` from a clean machine.

### One-time setup

Both indexes need a **pending publisher** before the first release — the mechanism for a project
that does not exist yet. It converts to a normal publisher on first upload.

| | PyPI | TestPyPI |
|---|---|---|
| Form | [pypi.org/manage/account/publishing](https://pypi.org/manage/account/publishing/) | [test.pypi.org/manage/account/publishing](https://test.pypi.org/manage/account/publishing/) |
| Project name | `ms-graph-mcp` | `ms-graph-mcp` |
| Owner / Repository | `nitin27may` / `ms-graph-mcp` | same |
| Workflow | `release.yml` | `release.yml` |
| Environment | `pypi` | `testpypi` |

The accounts are separate — a TestPyPI login is not a PyPI login. Without the TestPyPI publisher the
pipeline stops at its first stage.

Then create GitHub Environments `pypi` and `testpypi` under Settings → Environments, with a required
reviewer on `pypi` only, so a real publish pauses for approval and a candidate does not.

**There is no API token, and there should not be one.** Trusted Publishing exchanges a short-lived
GitHub OIDC token for an upload credential scoped to this workflow, so nothing long-lived exists to
leak. Note that signing in to PyPI *using* a GitHub account is unrelated and does not enable any of
this — the pending publisher above is what matters.
