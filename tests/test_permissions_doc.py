"""docs/permissions.md must match what the tools actually declare.

A permission matrix that drifts is worse than none: a reader grants what it says
and then hits SCOPE_DENIED with no idea why. The document is generated from the
tool descriptions, and this test fails if the committed copy has fallen behind —
so the drift is caught in CI rather than by someone following it.
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
SCRIPT = ROOT / "scripts" / "generate_permissions.py"
DOC = ROOT / "docs" / "permissions.md"


def _generator():
    """Load the generator by path.

    scripts/ is deliberately not a package — it must not end up in the wheel —
    so it cannot simply be imported.
    """
    spec = importlib.util.spec_from_file_location("generate_permissions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_doc_is_current():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, (
        f"{DOC.name} is stale. Run:\n"
        f"    uv run python scripts/generate_permissions.py\n\n"
        f"{result.stdout}{result.stderr}"
    )


def test_every_agent_tool_declares_a_permission():
    """A tool with no stated permission leaves a reader unable to grant for it."""
    permissions_for = _generator().permissions_for

    registry = get_registry()
    missing = [
        name
        for name in (*READ_TOOL_NAMES, *WRITE_TOOL_NAMES)
        if not permissions_for(registry.get(name).description)
    ]
    assert not missing, f"tools naming no delegated permission: {sorted(missing)}"


def test_the_read_consent_set_covers_every_read_tool():
    """The paste-in scope list must actually be sufficient.

    This is the failure the list exists to prevent: setup completes, sign-in
    succeeds, and then a tool returns SCOPE_DENIED for something the document
    never told you to grant.
    """
    permissions_for = _generator().permissions_for

    doc = DOC.read_text(encoding="utf-8")
    block = doc.split("### Read-only")[1].split("```")[1].strip()
    configured = set(block.split(","))

    registry = get_registry()
    needed: set[str] = set()
    for name in READ_TOOL_NAMES:
        needed.update(permissions_for(registry.get(name).description))

    assert not needed - configured, (
        f"read consent set omits {sorted(needed - configured)} — "
        "following it would produce SCOPE_DENIED"
    )


def test_no_application_permission_leaked_into_the_matrix():
    """This server is delegated-only; an app-only scope here would be a lie."""
    permissions_for = _generator().permissions_for

    registry = get_registry()
    suspicious: list[str] = []
    for name in (*READ_TOOL_NAMES, *WRITE_TOOL_NAMES):
        for perm in permissions_for(registry.get(name).description):
            # Nothing delegated is named like these.
            if perm.startswith(("Application.", "RoleManagement.", "Directory.ReadWrite")):
                suspicious.append(f"{name} -> {perm}")
    assert not suspicious, f"application-shaped permissions in the matrix: {suspicious}"
