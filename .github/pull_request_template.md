## What and why

<!-- What changes, and what problem it solves. Link the issue if there is one. -->

## Verification

<!-- Delete what does not apply. -->

- [ ] `uv run ruff check .` passes
- [ ] `uv run ruff format .` applied
- [ ] `uv run pytest -q` passes
- [ ] Tested against a real tenant

## If this adds or changes a tool

- [ ] Added to exactly one tuple in `allowlists.py`
- [ ] Read-tool count bumped in `tests/test_allowlists.py`
- [ ] Any caller-supplied value in a Graph path or `$filter` goes through `odata.py`
- [ ] Tests added against a mocked Graph
- [ ] Delegated permissions documented in the tool description

## If this touches auth or tier enforcement

- [ ] Write and internal tools still cannot reach the read surface
- [ ] The internal tier still gates on `is_machine`, not `is_app_only`
- [ ] Dispatch still fails closed on a missing token

## Notes for the reviewer

<!-- Anything you are unsure about, or deliberately left out. -->

- [ ] `CHANGELOG.md` updated under `## [Unreleased]`
