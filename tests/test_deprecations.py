"""The deprecation register is a promise; this is what enforces it.

Two directions, and both matter:

  - **Nothing outlives its removal version.** Once the package reaches a
    ``remove_in``, the build fails until the thing is actually removed or the
    date is moved on purpose. Without this a deprecation quietly becomes
    permanent, and the register turns into documentation of good intentions.
  - **Nothing in the register is fictional.** An entry describing something
    that no longer exists is worse than no entry — it implies a migration path
    that is not there.
"""

from __future__ import annotations

import pytest
from packaging.version import InvalidVersion, Version

from ms_graph_mcp.deprecations import DEPRECATIONS, Deprecation


def _current_version() -> Version:
    from ms_graph_mcp.server import _server_version

    return Version(_server_version())


class TestTheRegisterIsWellFormed:
    @pytest.mark.parametrize("dep", DEPRECATIONS, ids=lambda d: d.what[:40])
    def test_versions_parse(self, dep: Deprecation):
        try:
            Version(dep.deprecated_in), Version(dep.remove_in)
        except InvalidVersion as exc:
            pytest.fail(f"{dep.what}: {exc}")

    @pytest.mark.parametrize("dep", DEPRECATIONS, ids=lambda d: d.what[:40])
    def test_removal_comes_after_deprecation(self, dep: Deprecation):
        assert Version(dep.remove_in) > Version(dep.deprecated_in), (
            f"{dep.what} is marked for removal in {dep.remove_in}, which is not "
            f"after {dep.deprecated_in}"
        )

    @pytest.mark.parametrize("dep", DEPRECATIONS, ids=lambda d: d.what[:40])
    def test_a_replacement_is_named(self, dep: Deprecation):
        """A deprecation with no migration path is just a scheduled breakage."""
        assert dep.replacement.strip(), f"{dep.what} names no replacement"

    @pytest.mark.parametrize("dep", DEPRECATIONS, ids=lambda d: d.what[:40])
    def test_removal_is_never_a_patch_bump(self, dep: Deprecation):
        """Policy: a patch release never removes anything.

        Someone taking 0.2.0 -> 0.2.1 for a bug fix must not have working code
        break underneath them.
        """
        old, new = Version(dep.deprecated_in).release, Version(dep.remove_in).release
        assert new[:2] != old[:2], (
            f"{dep.what} would be removed in {dep.remove_in}, a patch bump from "
            f"{dep.deprecated_in}. Removals need at least a minor bump."
        )


class TestNothingHasOutlivedItsPromise:
    @pytest.mark.parametrize("dep", DEPRECATIONS, ids=lambda d: d.what[:40])
    def test_it_has_not_reached_its_removal_version(self, dep: Deprecation):
        """The point of the whole file.

        Compared on the release tuple, so entering the 0.4.0 cycle — 0.4.0rc1 —
        already counts as due. A prerelease is where the removal work belongs;
        discovering it at the final tag is too late to be comfortable.
        """
        current = _current_version().release
        due = Version(dep.remove_in).release

        assert current < due, (
            f"\n{dep.what}\n"
            f"  was due for removal in {dep.remove_in}; this package is now "
            f"{_current_version()}.\n"
            f"  Either remove it and delete the entry from deprecations.py, or "
            f"move `remove_in` on purpose.\n"
            f"  Replacement: {dep.replacement}"
        )


class TestTheRegisterMatchesReality:
    def test_the_tool_aliases_entry_is_real(self):
        """Guards the one entry that names a concrete mechanism.

        If the aliases were removed but the entry stayed, the register would be
        promising a compatibility path that no longer exists.
        """
        entry = [d for d in DEPRECATIONS if "alias" in d.what.lower()]
        if not entry:
            pytest.skip("no alias deprecation registered")

        import ms_graph_mcp  # noqa: F401
        from ms_graph_mcp.tooling import get_registry

        aliases = getattr(get_registry(), "_aliases", {})
        assert aliases, (
            "deprecations.py promises the old tool names still work, but the "
            "registry has no aliases registered"
        )

    def test_every_alias_resolves_to_a_real_tool(self):
        """An alias pointing at a renamed-again tool is a broken promise."""
        import ms_graph_mcp  # noqa: F401
        from ms_graph_mcp.tooling import get_registry

        registry = get_registry()
        dangling = [
            old
            for old, new in getattr(registry, "_aliases", {}).items()
            if registry.get(new) is None
        ]
        assert not dangling, f"aliases resolving to nothing: {sorted(dangling)}"
