#!/usr/bin/env python
"""Check every tool count quoted in prose against the code that defines it.

Counts in documentation drift silently. Nothing fails when a tool is added and
`README.md` still says 60 — the reader simply gets a number that was true once.
This derives each of them from the allowlists, the registry and the profile
definitions, so a stale one fails the pull request instead of shipping.

`tests/test_doc_counts.py` covers the same checks minus the test count, which
needs a pytest collection pass and does not belong inside the suite it counts.

    uv run python scripts/check_docs.py          # rewrite the stale numbers
    uv run python scripts/check_docs.py --check  # exit 1 if any is stale

A check that matches **nowhere** is also a failure. Rewording the sentence a
pattern anchors on would otherwise leave the check silently watching nothing,
which is the same drift wearing a different hat.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import ms_graph_mcp  # noqa: F401  (import registers every @tool)
from ms_graph_mcp.allowlists import (
    INTERNAL_TOOL_NAMES,
    READ_TOOL_NAMES,
    WRITE_TOOL_NAMES,
)
from ms_graph_mcp.deprecations import DEPRECATIONS
from ms_graph_mcp.tooling import get_registry
from ms_graph_mcp.toolsets import ALL_PROFILE, PROFILES, filter_tool_names

ROOT = Path(__file__).resolve().parent.parent

# Every file allowed to quote a derived count. A pattern is applied to all of
# them; a file that never mentions the thing simply contributes no matches.
SCANNED = (
    "README.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "docs/graph-coverage.md",
    "docs/testing.md",
    "docs/roadmap.md",
    "docs/README.md",
    "pyproject.toml",
    "src/ms_graph_mcp/deprecations.py",
    "src/ms_graph_mcp/toolsets.py",
    "tests/test_errors.py",
    "tests/test_tools_contract.py",
    "tests/test_protocol_conformance.py",
)


@dataclass(frozen=True)
class Check:
    """One derived number, and every phrasing that may quote it.

    ``patterns`` each capture the number as group ``n``. Several phrasings map
    to one value on purpose — prose should read naturally, and forcing one
    sentence shape everywhere would be a worse document for a better script.
    """

    label: str
    expected: int
    patterns: tuple[str, ...]


def _agent_visible() -> tuple[str, ...]:
    return READ_TOOL_NAMES + WRITE_TOOL_NAMES


def _namespace_counts() -> Counter[str]:
    return Counter(name.split("_", 1)[0] for name in _agent_visible())


def _profile_read_count(profile: str) -> int:
    return len(filter_tool_names(READ_TOOL_NAMES, profile))


def _checks() -> list[Check]:
    total = len(get_registry().names())
    checks = [
        Check(
            "total tools",
            total,
            (
                r"(?P<n>\d+) tools across",
                r"Current surface: \*\*(?P<n>\d+) tools\*\*",
                r"\b(?P<n>\d+)-tool surface\b",
                r"(?P<n>\d+) tools is a lot to put in front of a model",
                r"not (?P<n>\d+) of them",
                r"so (?P<n>\d+) tools need not",
                r"the existing (?P<n>\d+) tools",
            ),
        ),
        Check(
            "read tools",
            len(READ_TOOL_NAMES),
            (
                r"\| \*{0,2}Read\*{0,2} \| (?P<n>\d+) \|",
                r"\*\*\d+ tools\*\* — (?P<n>\d+) read",
            ),
        ),
        Check(
            "write tools",
            len(WRITE_TOOL_NAMES),
            (
                r"\| \*{0,2}Write\*{0,2} \| (?P<n>\d+) \|",
                r"\d+ read, (?P<n>\d+) write",
                r"the (?P<n>\d+) write tools",
            ),
        ),
        Check(
            "internal tools",
            len(INTERNAL_TOOL_NAMES),
            (
                r"\| \*{0,2}Internal\*{0,2} \| (?P<n>\d+) \|",
                r"\d+ write, (?P<n>\d+) internal",
            ),
        ),
        Check(
            "agent-visible tools",
            len(_agent_visible()),
            (r"(?P<n>\d+) agent-visible",),
        ),
        Check(
            "pre-namespace aliases",
            len(get_registry().aliases()),
            (r"the (?P<n>\d+) pre-namespace tool aliases",),
        ),
        Check(
            "registered deprecations",
            len(DEPRECATIONS),
            (r"(?P<n>\d+) registered deprecation",),
        ),
    ]

    # The README's namespace breakdown: "mail 11 · tasks 11 · calendar 10 · …".
    for namespace, count in sorted(_namespace_counts().items()):
        checks.append(
            Check(
                f"namespace {namespace}",
                count,
                (rf"\b{namespace} (?P<n>\d+) ·", rf"· {namespace} (?P<n>\d+)\b"),
            )
        )

    # The "Read tools" column of the toolset-profile table. Anchored on the
    # backticked profile name in the first cell, so reordering rows is safe.
    for profile in (*sorted(PROFILES), ALL_PROFILE):
        checks.append(
            Check(
                f"profile {profile}",
                _profile_read_count(profile),
                (rf"\| `{profile}`[^|\n]*\|[^|\n]*\| *(?P<n>\d+) *\|",),
            )
        )

    return checks


def _collected(path: str) -> int:
    """How many tests pytest collects under ``path``."""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", path, "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    match = re.search(r"(\d+) tests collected", out)
    if match is None:  # pragma: no cover - pytest changed its summary line
        raise RuntimeError(f"could not read the collected test count for {path}")
    return int(match.group(1))


def _test_count_checks() -> list[Check]:
    """Test counts quoted in ``docs/testing.md``.

    Kept out of ``_checks()`` because they shell out to pytest — which is also
    why ``tests/test_doc_counts.py`` does not cover them. A suite that collects
    itself to assert its own size is a loop worth avoiding.

    The per-area numbers in the arrangement table drift the same way the total
    does: adding four tests to ``tests/entra/`` has no reason to touch the
    sentence quoting 56.
    """
    return [
        Check("tests", _collected("tests"), (r"^(?P<n>\d+) tests, all offline",)),
        Check(
            "tool-contract tests",
            _collected("tests/test_tools_contract.py"),
            (r"`test_tools_contract\.py` \((?P<n>\d+)\)",),
        ),
        Check(
            "protocol tests",
            _collected("tests/test_protocol_conformance.py"),
            (r"`test_protocol_conformance\.py` \((?P<n>\d+)\)",),
        ),
        Check(
            "entra tests",
            _collected("tests/entra"),
            (r"`tests/entra/` \((?P<n>\d+)\)",),
        ),
    ]


def _apply(text: str, checks: list[Check]) -> tuple[str, list[str], set[str]]:
    """Rewrite every quoted count, reporting what was stale and what matched."""
    stale: list[str] = []
    seen: set[str] = set()

    for check in checks:
        for pattern in check.patterns:
            for match in list(re.finditer(pattern, text, re.MULTILINE)):
                seen.add(check.label)
                found = match.group("n")
                if int(found) == check.expected:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                stale.append(f"line {line}: {check.label} says {found}, code says {check.expected}")
                start, end = match.span("n")
                text = text[:start] + str(check.expected) + text[end:]

    return text, stale, seen


def main() -> int:
    checks = [*_checks(), *_test_count_checks()]
    check_only = "--check" in sys.argv

    stale_total = 0
    matched: set[str] = set()

    for relative in SCANNED:
        path = ROOT / relative
        if not path.exists():
            continue
        original = path.read_text(encoding="utf-8")
        rewritten, stale, seen = _apply(original, checks)
        matched |= seen

        for problem in stale:
            print(f"{relative}:{problem}")
        stale_total += len(stale)

        if stale and not check_only:
            path.write_text(rewritten, encoding="utf-8")

    # A pattern that matched nothing anywhere is watching a sentence that no
    # longer exists. Silent from then on, so it fails loudly now.
    blind = sorted(check.label for check in checks if check.label not in matched)
    for label in blind:
        print(f"no file quotes '{label}' — its pattern matches nothing")

    if stale_total or blind:
        if check_only:
            print(
                f"\n{stale_total} stale value(s), {len(blind)} unmatched check(s) — "
                "run scripts/check_docs.py"
            )
            return 1
        if stale_total:
            print(f"\nrewrote {stale_total} stale value(s)")
        return 1 if blind else 0

    print(f"{len(checks)} documented counts are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
