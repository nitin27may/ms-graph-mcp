# ADR 0001 — Keep the `src/ms_graph_mcp/` package layout

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

The repository stores its code at `src/ms_graph_mcp/`. During the open-source release planning it
was proposed to remove the inner `ms_graph_mcp/` directory and place the modules directly under
`src/`, on the reasoning that this repository *is* the MCP server, so the extra folder looks
redundant.

That reasoning is sound for an application. It does not hold for a distribution, and this is a
distribution: the package is published to PyPI and consumed as `uvx --from ms-graph-mcp
ms-graph-mcp` or `pip install ms-graph-mcp`.

## Decision

Keep `src/ms_graph_mcp/`.

## Consequences

Python has no separate notion of an import name — the directory name *is* the import name. Three
things follow directly:

1. **Flattening makes the import name `src`.** `from ms_graph_mcp.client import graph_get` would
   become `from src.client import graph_get`, or the modules would have to be registered
   individually via `py-modules`. Either way the published distribution occupies a name every other
   `src`-layout project also wants, and `import ms_graph_mcp` — the name in every README, config
   snippet and downstream import — stops existing.

2. **`src/` is not the redundant part; it is the part doing work.** With a src-layout, the package
   is not importable from the repository root, so `uv run pytest` exercises the *installed*
   package rather than the working tree. A module missing from the wheel fails in CI, not after
   release. A flat layout (`ms_graph_mcp/` at the repository root) is a legitimate alternative that
   keeps a real package name while dropping this guarantee; it was considered and rejected because
   packaging correctness matters more here than one directory level.

3. **`test_package_is_self_contained`** (`tests/test_tools_contract.py`) walks
   `ms_graph_mcp.__path__` to prove no module reaches back into the monorepo this package was
   extracted from. That guard is written against a single package root and would have to be
   rewritten for a flat set of top-level modules.

The cost of the decision is one extra directory level when navigating the tree. That is the whole
cost.

## References

- Python Packaging User Guide — [src layout vs flat layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)
- `pyproject.toml` — `[tool.hatch.build.targets.wheel] packages = ["src/ms_graph_mcp"]`
