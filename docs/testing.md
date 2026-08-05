# Testing

855 tests, all offline. No Graph call is ever made, no network is touched, and the whole suite runs
in under two seconds — so there is no reason to skip it before pushing.

```bash
uv run pytest -q                            # everything
uv run pytest tests/test_meetings.py -q     # one file
uv run pytest -k "write_scope" -q           # one pattern
uv run pytest -q -x --lf                    # stop at first failure, rerun last failures
```

Then, always:

```bash
uv run ruff check .        # NB: [tool.ruff] fix = true — this rewrites files
uv run ruff format .       # CI runs --check, so an unformatted tree fails the PR
```

There is no typecheck step — no mypy or pyright is configured — so "verified" here means **pytest
and ruff, both green**. CI runs exactly that on Python 3.12 and 3.13, then builds the wheel,
installs it into a clean venv and resolves the tool allowlists from it, so a module missing from the
wheel fails the PR rather than a user's first install.

## How the suite is arranged

| Area | Files | What it protects |
|---|---|---|
| Tool contract | `test_tools_contract.py` (326) | Annotations, description length, error discipline — every tool, parametrized |
| Security tiers | `test_security_defaults.py`, `test_internal_tier.py`, `test_allowlists.py` | Read/write/internal separation, `GRAPH_MCP_READ_ONLY`, the machine principal |
| Protocol | `test_protocol_conformance.py` (16) | The wire format, via a real client session |
| Auth | `tests/entra/` (56) | Token validation against a real RS256 keypair |
| Transport | `test_app.py`, `test_auth.py`, `test_oauth_discovery.py` | Middleware, discovery, host policy |
| Domains | `test_meetings.py`, `test_calendar_write.py`, … | Per-tool behaviour against mocked Graph responses |

Two pytest settings shape how tests are written:

- **`asyncio_mode = "auto"`** — async tests need no `@pytest.mark.asyncio`.
- **`--import-mode=importlib`** — lets `tests/test_config.py` and `tests/entra/test_config.py`
  coexist without `__init__.py` files.

## The three that catch what the others cannot

### `test_tools_contract.py` — drift, on every tool at once

A 90-tool surface only stays coherent if drift breaks the build. This file parametrizes over the
whole registry, so a new tool is held to the same standard as the first one without anybody
remembering to add a test. It enforces that every tool declares annotations, that descriptions are
200–400 characters and differentiate the tool from its neighbours, and that failures come from
`errors.py` rather than ad-hoc dicts.

Descriptions are the only thing a model selects on. Terseness is not a token saving — it is the main
cause of mis-selection, which costs far more than the characters saved.

### `test_protocol_conformance.py` — the only place the wire protocol runs

Every other test calls handlers directly. That proves the *logic* but not that the server speaks
MCP: a handler can return a well-formed Python object and still fail schema validation on the wire,
or negotiate down to a legacy protocol revision and silently drop fields.

`mcp.Client` accepts a `Server` instance and runs a real session in-process — real `initialize`
negotiation, real request/response validation, no HTTP and no network. Cheap enough to keep in the
unit suite, and the only thing that catches a schema rejection.

It pins the protocol revision (`2026-07-28`). If a future SDK negotiates lower by default, that is a
regression worth hearing about rather than absorbing.

### `tests/entra/` — real signature verification, no network

`tests/entra/conftest.py` generates a real RS256 keypair and patches
`jwt_verify.get_jwks_client` to return the matching public key. The actual `jwt.decode` path runs,
including signature verification, issuer and audience checks.

**Extend these fixtures rather than mocking `verify_token`.** Mocking the verifier tests that the
call happens; it does not test that a forged token is rejected, which is the only property that
matters.

## Gotchas

These cost real time when hit cold.

- **MCP SDK 2.x models expose *field names*, not aliases.** `tool.input_schema`,
  `result.is_error`, `result.ttl_ms`. Writing `tool.inputSchema` raises `AttributeError`. The
  camelCase forms work only as *construction* keywords (`types.Tool(inputSchema=...)`) and on the
  wire. In a test this surfaces as an unrelated-looking `AttributeError`.
- **`Server.get_request_handler()` takes a method string** — `get_request_handler("tools/list")`,
  not `get_request_handler(types.ListToolsRequest)`. The type form returns `None`, and the failure
  appears later as "NoneType is not callable".
- **Restore `current_request_context` by value, not by token.** A fixture body and an async test run
  in different contexts, and `ContextVar.reset()` rejects a token minted in another one. See the
  fixture in `test_protocol_conformance.py`.
- **`get_config()` is a cached singleton and `build_app()` mutates it.** `tests/conftest.py` resets
  it autouse; keep config-touching tests within that fixture's reach.
- **The autouse fixture pins `toolsets = "all"`.** The shipped default is `core`, which would mean
  every tier test was quietly asserting profile filtering too. Tests about profiles set their own
  config.
- **`build_app()` is a factory, not a singleton.** A session manager's `run()` may only be entered
  once, so each test needs its own app.
- **A `TestClient` must present an acceptable `Host`.** The transport validates it, and TestClient's
  default `testserver` is not on the list — the request dies with `421` before reaching the code
  under test. Pass `base_url` (see `test_oauth_discovery.py`).

## Adding a tool

Four steps, in `CLAUDE.md` in full. Skipping either of the last two breaks the build:

1. Pydantic input model + an **async** function with `@tool(description=...)`.
2. Add the name to exactly one tuple in `allowlists.py`. A tool in no allowlist is unreachable; an
   allowlist name with no tool raises `RuntimeError` on the next `tools/list`.
3. Bump the hardcoded count in `tests/test_allowlists.py` — so adding a tool is a deliberate edit.
4. `uv run python scripts/generate_permissions.py` to regenerate `docs/permissions.md`. CI checks
   this with `--check`.

## MCP Inspector

The reference client. Use it to confirm a real MCP client sees what you think it does — pytest
proves the handlers work, Inspector proves a client can use them.

Requires **Node 22.19+**.

### CLI — scriptable, good for CI

```bash
npx @modelcontextprotocol/inspector --cli uv run ms-graph-mcp --method tools/list
```

```bash
# Call a tool. Arguments are repeated --tool-arg key=value pairs.
npx @modelcontextprotocol/inspector --cli uv run ms-graph-mcp \
  --method tools/call --tool-name people_get_my_profile
```

Without credentials that call returns the structured refusal rather than crashing, which is itself
worth checking:

```json
{"error": "missing_graph_token", "message": "No Graph access token was supplied. Set GRAPH_MCP_CLIENT_ID …"}
```

> **Inspector does not inherit your shell environment.** Exporting `GRAPH_MCP_TOOLSETS` and then
> running Inspector silently gets you the *default* profile, not the one you set — an easy hour lost
> to debugging a filter that was never applied. Pass variables explicitly with `-e`, **after** the
> server command:
>
> ```bash
> npx @modelcontextprotocol/inspector --cli \
>   uv run ms-graph-mcp -e GRAPH_MCP_TOOLSETS=mail -e GRAPH_MCP_ACCESS_TOKEN=… \
>   --method tools/list
> ```
>
> Placing `-e` before `--cli` launches the web UI instead of running the command.

### Web UI — exploratory

```bash
npx @modelcontextprotocol/inspector uv run ms-graph-mcp
```

Opens a browser with the tool list, schema forms and a request/response log. Best for reading a
schema as a client renders it and for eyeballing whether a description actually differentiates a
tool from its neighbour.

Against the HTTP transport, start the server first and connect to `http://localhost:8094/mcp` with
transport **Streamable HTTP**, adding `Authorization: Bearer …` under the auth settings.

### A smoke check for CI

Exercises a real client, so it catches protocol regressions the direct-call tests cannot:

```bash
npx @modelcontextprotocol/inspector --cli uv run ms-graph-mcp \
  -e GRAPH_MCP_ACCESS_TOKEN=not-a-real-token --method tools/list \
  | python -c 'import json,sys; n=len(json.load(sys.stdin)["tools"]); print(f"{n} tools"); sys.exit(0 if n else 1)'
```

The token is never used — `tools/list` does not call Graph — but its presence keeps the server from
attempting an interactive sign-in it cannot complete in CI.

## Verifying against a real tenant

Everything above proves the shapes are right. None of it proves Graph accepts them, and payload
shapes that only fail against the live API are the specific risk — `calendar_create_event`'s nested
attendees, the Planner `If-Match` etag dance, `search_query`'s entity-type combinations.

Before a release, exercise at least one tool per namespace against live Graph, confirm a write tool
is refused without `GRAPH_MCP_WRITE_SCOPE`, and confirm `GRAPH_MCP_READ_ONLY` removes the tier. See
[debugging.md](debugging.md) for reading what comes back.
