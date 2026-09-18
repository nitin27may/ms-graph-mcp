"""The register of things that are deprecated, and when they go away.

A deprecation is a promise with a date on it. Left as prose in a changelog, the
promise is easy to make and easy to forget — the thing keeps working, nobody is
reminded, and it either lives forever or disappears in a release that was not
supposed to break anyone.

Recording them here makes the promise checkable. ``tests/test_deprecations.py``
fails the build once the current version reaches a ``remove_in``, so removal
becomes a deliberate edit rather than something that depends on memory. Pushing
a date back is allowed — it just has to be a decision someone makes on purpose.

Deliberately stdlib-only. ``test_package_imports_nothing_undeclared`` checks
every import in this package against the declared dependencies, and the version
comparison this data exists for belongs in the test rather than here.

The policy these entries follow is in CONTRIBUTING.md. In short: nothing is ever
removed in a patch release; pre-1.0 a deprecation survives at least one minor
release, post-1.0 until the next major.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DEPRECATIONS", "Deprecation"]


@dataclass(frozen=True)
class Deprecation:
    """One deprecated thing, and the release that removes it."""

    what: str
    """What is deprecated, specifically enough to find it in the code."""

    replacement: str
    """What to use instead. A deprecation without a migration path is a removal."""

    deprecated_in: str
    """The version that started the clock."""

    remove_in: str
    """The version that removes it. The build fails once this is reached."""

    note: str = ""
    """Why the date is what it is, when that is not obvious."""


DEPRECATIONS: tuple[Deprecation, ...] = (
    Deprecation(
        what="the 51 pre-namespace tool aliases (`search_mail`, `create_folder`, …)",
        replacement="namespace-prefixed names — `mail_search`, `files_create_folder`, …",
        deprecated_in="0.2.0",
        remove_in="0.4.0",
        note=(
            "The rename predates any public release, so the old names were only ever "
            "reachable from the private codebase this was extracted from — nobody "
            "outside it can be depending on them. The 0.2.0 changelog nevertheless "
            "published 'until 0.3.0', and honouring that generously costs nothing, "
            "whereas breaking it in the very next release would not look deliberate. "
            "Aliases are already absent from tools/list, so they cost no context."
        ),
    ),
    Deprecation(
        what="`X-Write-Scope: true` as the sole grant of write authority",
        replacement=(
            "the `access_as_user.write` delegated scope in the token's `scp` claim "
            "(`GRAPH_MCP_WRITE_SCOPE_NAME`); the header may still narrow it"
        ),
        deprecated_in="0.4.0",
        remove_in="0.5.0",
        note=(
            "A header is something the caller sets for itself, so it was never "
            "authority — anyone who could reach the server could send it. The "
            "header keeps working: the rule is `header AND scope`, so a client can "
            "still decline write access it holds. What goes away in 0.5.0 is the "
            "header deciding alone. Only the resource-server posture is affected; "
            "in passthrough the token is audienced to Graph and its `scp` carries "
            "Graph permissions rather than scopes this server defines, so there is "
            "nothing to check against."
        ),
    ),
    Deprecation(
        what="token passthrough (`GRAPH_MCP_DOES_OBO=false`)",
        replacement=(
            "the resource-server posture (the default): a token audienced to this "
            "server, which it exchanges for a Graph token itself"
        ),
        deprecated_in="0.4.0",
        remove_in="1.0.0",
        note=(
            "Accepting a token minted for Microsoft Graph is the confused-deputy "
            "anti-pattern the MCP authorization specification names, and it cannot "
            "satisfy a Conditional Access step-up — the claims challenge has nowhere "
            "to go. The removal date is deliberately distant rather than one minor "
            "cycle: this is a deployment's whole auth topology, not a header, and the "
            "platform this server was extracted from still runs it. It stays working "
            "and warns at startup until 1.0.0."
        ),
    ),
)
