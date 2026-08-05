"""Every tool count quoted in prose must match the code that defines it.

A count in a document is a claim with nothing holding it up. "60 tools" stayed
in `CLAUDE.md` through two releases that took the surface to 85, because adding
a tool never had a reason to touch the sentence. `scripts/check_docs.py` derives
each number from the allowlists, the registry and the profile definitions; this
runs it, so the drift fails a pull request rather than reaching a reader.

The test count in `docs/testing.md` is checked by the script but not here — it
needs a pytest collection pass, which does not belong inside the suite it is
counting.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import ms_graph_mcp  # noqa: F401  (registers every @tool)
from ms_graph_mcp.allowlists import READ_TOOL_NAMES, WRITE_TOOL_NAMES
from ms_graph_mcp.tooling import get_registry

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_docs.py"


def _checker():
    """Load the checker by path.

    scripts/ is deliberately not a package — it must not end up in the wheel —
    so it cannot simply be imported.

    It must be in ``sys.modules`` *before* it executes: the module declares a
    dataclass under ``from __future__ import annotations``, and resolving those
    string annotations goes through ``sys.modules[cls.__module__]``. Absent, it
    fails as an ``AttributeError`` on ``None`` from inside dataclasses.
    """
    spec = importlib.util.spec_from_file_location("check_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_documented_counts_are_current():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, (
        "documented counts have drifted from the code. Run:\n"
        "    uv run python scripts/check_docs.py\n\n"
        f"{result.stdout}{result.stderr}"
    )


def test_every_check_matches_something():
    """A pattern anchored on a sentence nobody writes any more watches nothing.

    This is the failure mode that lets drift back in quietly: the number stops
    being checked, and the check keeps passing. Rewording a sentence must break
    the build so the pattern is updated with it.
    """
    module = _checker()
    checks = module._checks()

    matched: set[str] = set()
    for relative in module.SCANNED:
        path = ROOT / relative
        if path.exists():
            _, _, seen = module._apply(path.read_text(encoding="utf-8"), checks)
            matched |= seen

    blind = sorted(check.label for check in checks if check.label not in matched)
    assert not blind, f"checks matching no documented text: {blind}"


def test_the_counts_are_derived_rather_than_restated():
    """The script must read the code, not a second copy of the same numbers.

    A checker with its own hardcoded totals is one more place to forget, so
    every expected value has to come back to the allowlists and the registry.
    """
    module = _checker()
    by_label = {check.label: check.expected for check in module._checks()}

    assert by_label["total tools"] == len(get_registry().names())
    assert by_label["read tools"] == len(READ_TOOL_NAMES)
    assert by_label["write tools"] == len(WRITE_TOOL_NAMES)
    assert by_label["agent-visible tools"] == len(READ_TOOL_NAMES) + len(WRITE_TOOL_NAMES)
    assert by_label["pre-namespace aliases"] == len(get_registry().aliases())
