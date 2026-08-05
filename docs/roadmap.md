# Roadmap

What is **not** done. Shipped work lives in [CHANGELOG.md](../CHANGELOG.md) — a list of ticked boxes
is history, not a plan, so items are deleted from here when they land rather than crossed out.

Nothing here has a date. This is a side project with a public tenant behind it; ordering is a
statement of intent, not a commitment. Issues and pull requests against any of it are welcome —
start with [CONTRIBUTING.md](../CONTRIBUTING.md).

## Graph coverage

Derived from the gap analysis in [graph-coverage.md](graph-coverage.md), which has the endpoint
detail and the reasoning. Ranked by value per unit of work.

| | Item | Approx. tools | Why it is where it is |
|---|---|---:|---|
| 1 | **SharePoint sites and lists** — find a site, enumerate lists, read and write list items | 5–6 | The largest absent workload. `listItem` with `?expand=fields` is a general-purpose structured-data reader, and `Sites.Read.All` is already in the read consent set. |
| 2 | **Directory completion** — `directReports`, `transitiveMemberOf`, profile photo, `checkMemberGroups` | 3–4 | `directory_get_user_manager` exists; its inverse does not, so org-chart traversal only runs upward. |
| 3 | **File operations** — move, copy, delete, list and revoke permissions, versions | 4–5 | `files_create_sharing_link` can hand out access that nothing here can withdraw. |
| 4 | **Large-file upload session** — `createUploadSession` | 1 | `files_upload` is single-shot base64, so file size is bounded by the request. |
| 5 | **Mail folders and drafts** | 4–5 | A draft an agent prepares for review is a materially safer default than a send. |
| 6 | **Presence** — `/me/presence`, `getPresencesByUserId` | 1–2 | Small, and pairs directly with `calendar_get_free_busy`. |
| 7 | **Teams channel posting and message replies** | 2–3 | An agent can post to a chat but not to a channel, which is an odd asymmetry to leave. |
| 8 | **Excel workbook**, **change notifications / delta**, **`$batch`** | — | Larger and cross-cutting. `$batch` is a pure efficiency win; delta is the prerequisite for any sync feature, and the only sanctioned way to track Teams messages. |

Adding all of 1–7 takes the surface past 100 tools, which is what makes the
[toolset profiles](../README.md#toolset-profiles) load-bearing rather than a nicety.

## Platform

- **Sovereign clouds** — GCC High, DoD and 21Vianet. A few Graph and login endpoints are still
  hardcoded to the commercial cloud, so those tenants cannot use this at all today.
- **`httpx` / `httpx2` consolidation.** `mcp` 2.0 runs on `httpx2`, a distribution separate from the
  `httpx` the Graph client uses, so both are installed. Deliberately not bundled with the SDK
  migration — an HTTP-stack swap and an SDK upgrade are two failure domains and do not belong in one
  change. See the note in the `dependencies` block of `pyproject.toml`.
- **A typecheck step.** There is no mypy or pyright configured, so "verified" currently means pytest
  and ruff. The package ships `py.typed` and is annotated throughout; nothing enforces that.

## Before 1.0

Pre-1.0, the configuration surface may change between minor versions. These are what have to settle
before it is frozen under [semver](https://semver.org/spec/v2.0.0.html):

- **Environment variable names.** `GRAPH_MCP_*` is stable in shape, but the auth-posture settings
  (`GRAPH_MCP_DOES_OBO`, `GRAPH_MCP_AUDIENCE`) exist to describe two deployment models that may
  collapse into one once RFC 8707 audience binding is universal in MCP clients.
- **The internal tier.** Nine tools reachable only by a shared-secret machine principal, extracted
  from the platform this was built for. Whether they stay in this package or move out is an open
  question.
- **Removing the pre-namespace aliases.** 51 of them, due in `0.4.0`. See the deprecation policy in
  [CONTRIBUTING.md](../CONTRIBUTING.md#deprecation-policy).

## Not planned

Recorded so they are not repeatedly proposed. The reasoning is in
[graph-coverage.md](graph-coverage.md#out-of-scope--and-why).

- Microsoft Loop — no generally available API to build on.
- Planner Premium / Project for the web — excluded by Microsoft from the Planner API.
- Application-permission surfaces: call records, Intune, Identity Protection, audit logs, usage
  reports. This server is delegated-only by design.
- Directory writes — creating users, resetting passwords, managing group membership. This is an
  assistant, not an IAM tool.
- A gateway-trust mode that skips token validation. See
  [ADR 0003](adr/0003-no-gateway-trust-mode.md); a test asserts no such setting exists.
