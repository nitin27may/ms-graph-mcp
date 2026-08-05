"""Bump the version and roll the changelog, ready for a release tag.

Run by `.github/workflows/release-prep.yml`, which opens a pull request with the
result rather than committing to main — the diff is small and worth a look
before it becomes a tag that cannot be taken back.

Usable by hand too:

    uv run python scripts/prepare_release.py 0.2.0
    uv run python scripts/prepare_release.py 0.2.0rc1 --date 2026-08-05

A prerelease bumps the version but leaves `## [Unreleased]` alone. A candidate
is a rehearsal, not a release: cutting a dated changelog section for it would
either strand the entries under a version nobody installs, or force them to be
written twice.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import sys

from packaging.version import InvalidVersion, Version

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"

UNRELEASED = "## [Unreleased]"


def bump_pyproject(version: str) -> str:
    """Rewrite the `version = ` line in the `[project]` table.

    Anchored to the first occurrence, which is the project's own version. A
    naive replace of the string would also hit any dependency pin that happened
    to carry the same number.
    """
    text = PYPROJECT.read_text()
    new, count = re.subn(
        r'^(version\s*=\s*)"[^"]+"',
        rf'\1"{version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        sys.exit("::error::could not find the version line in pyproject.toml")
    PYPROJECT.write_text(new)
    return new


def roll_changelog(version: str, when: str) -> bool:
    """Turn `## [Unreleased]` into a dated section and open a fresh one.

    Returns False when there is nothing to roll, so the caller can say so
    rather than opening an empty release.
    """
    text = CHANGELOG.read_text()
    if UNRELEASED not in text:
        sys.exit(f"::error::{CHANGELOG.name} has no '{UNRELEASED}' heading to roll")

    head, _, tail = text.partition(UNRELEASED)
    body, sep, rest = tail.partition("\n## [")
    if not body.strip():
        return False

    rolled = f"{head}{UNRELEASED}\n\n## [{version}] - {when}{body}{sep}{rest}"
    CHANGELOG.write_text(_relink(rolled, version))
    return True


def _relink(text: str, version: str) -> str:
    """Keep the reference definitions at the foot of the file in step.

    Keep a Changelog headings are reference links, so a new `## [0.2.0]` section
    renders as literal brackets until `[0.2.0]:` exists. Easy to miss, because
    the file looks right in a plain-text diff and only breaks once rendered.
    """
    match = re.search(r"^\[Unreleased\]:\s*(\S*/compare/)v(\S+)\.\.\.HEAD\s*$", text, re.MULTILINE)
    if not match:
        # No reference-link section, or a layout this does not recognise. Leave
        # it alone rather than guessing and corrupting it.
        return text

    compare_url, previous = match.group(1), match.group(2)
    return text.replace(
        match.group(0),
        f"[Unreleased]: {compare_url}v{version}...HEAD\n"
        f"[{version}]: {compare_url}v{previous}...v{version}",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="the new version, e.g. 0.2.0 or 0.2.0rc1")
    parser.add_argument("--date", default=dt.date.today().isoformat())
    args = parser.parse_args()

    try:
        parsed = Version(args.version)
    except InvalidVersion as exc:
        sys.exit(f"::error::{exc}")

    # Normalizing means `0.2.0-rc1` and `0.2.0rc1` cannot produce two different
    # files, and that what lands in pyproject.toml is exactly what the release
    # workflow will compare the tag against.
    version = str(parsed)

    current = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT.read_text(), re.MULTILINE)
    if current and Version(current.group(1)) == parsed:
        sys.exit(f"::error::pyproject.toml is already {version}")
    if current and Version(current.group(1)) > parsed:
        sys.exit(f"::error::{version} is older than the current {current.group(1)}")

    bump_pyproject(version)
    print(f"pyproject.toml -> {version}")

    if parsed.is_prerelease:
        print("prerelease: leaving [Unreleased] intact — the changelog is cut at the final release")
    elif roll_changelog(version, args.date):
        print(f"CHANGELOG.md  -> [{version}] - {args.date}")
    else:
        print("::warning::[Unreleased] was empty; no changelog section written")


if __name__ == "__main__":
    main()
