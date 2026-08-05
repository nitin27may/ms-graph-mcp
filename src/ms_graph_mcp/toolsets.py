"""Named toolset profiles, so a client sees the tools it needs and not 85 of them.

A profile is a set of **namespace prefixes**, not a hand-maintained list of tool
names. Namespaces already track Graph permission families (`mail_`, `calendar_`,
`files_`, …), so filtering is a prefix match and a newly added tool joins its
profile automatically rather than being forgotten.

Two places select profiles:

  ``GRAPH_MCP_TOOLSETS``   at startup — the deployment's decision
  ``X-Toolsets`` header    per request — the caller's decision

**The startup value is a ceiling.** A request may narrow it and can never widen
it, so a client asking for ``all`` gains nothing the operator did not already
grant. That is what makes the header safe to honour from an untrusted caller.

**This filters visibility, not authority.** Hiding a tool from ``tools/list`` is
a context-efficiency measure — a caller can still name any tool it likes. The
tier gates in ``server.py`` and ``assert_no_write_in_reads()`` remain the things
that actually stop a write, and they are unchanged by anything here.
"""

from __future__ import annotations

__all__ = [
    "ALL_PROFILE",
    "PROFILES",
    "UnknownToolsetError",
    "filter_tool_names",
    "namespace_of",
    "parse_toolsets",
    "resolve_namespaces",
]


class UnknownToolsetError(ValueError):
    """A profile name that does not exist. Raised rather than ignored."""


#: Selects every namespace. Kept separate from the mapping so it cannot be
#: confused with a real profile that happens to list everything.
ALL_PROFILE = "all"

# Each profile maps to the namespace prefixes it exposes. `core` is the default:
# a cross-workload subset that answers most questions without loading the whole
# surface — the workloads people actually reach for first, plus search as the
# entry point for anything vague.
PROFILES: dict[str, frozenset[str]] = {
    "core": frozenset({"search", "mail", "calendar", "files", "people"}),
    "search": frozenset({"search"}),
    "mail": frozenset({"mail"}),
    "calendar": frozenset({"calendar"}),
    "meetings": frozenset({"meetings", "calendar"}),
    "files": frozenset({"files"}),
    "chat": frozenset({"chat"}),
    "people": frozenset({"people"}),
    "directory": frozenset({"directory", "people"}),
    "tasks": frozenset({"tasks"}),
    "notes": frozenset({"notes"}),
}

#: Every namespace any profile can reach, plus the internal one. Used to expand
#: ``all`` without having to restate the list.
_EVERY_NAMESPACE: frozenset[str] = frozenset().union(*PROFILES.values()) | {"graph"}

DEFAULT_PROFILE = "core"


def namespace_of(tool_name: str) -> str:
    """The namespace prefix of a tool name — ``mail_search`` -> ``mail``."""
    return tool_name.split("_", 1)[0]


def parse_toolsets(raw: str | None) -> list[str]:
    """Split a comma-separated profile list, ignoring blanks and case."""
    if not raw:
        return []
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def resolve_namespaces(profiles: list[str]) -> frozenset[str]:
    """Expand profile names to the namespaces they cover.

    An unknown name raises rather than being skipped: silently ignoring a typo
    would serve a surface nobody asked for, and the operator would have no
    signal that their configuration did not take effect.
    """
    if not profiles:
        profiles = [DEFAULT_PROFILE]
    if ALL_PROFILE in profiles:
        return _EVERY_NAMESPACE

    unknown = [p for p in profiles if p not in PROFILES]
    if unknown:
        raise UnknownToolsetError(
            f"unknown toolset(s) {sorted(unknown)}; valid names are "
            f"{sorted([*PROFILES, ALL_PROFILE])}"
        )
    return frozenset().union(*(PROFILES[p] for p in profiles))


def filter_tool_names(
    names: tuple[str, ...] | list[str],
    startup: str | None,
    requested: str | None = None,
) -> list[str]:
    """Names visible for this request, given the startup ceiling and any override.

    ``requested`` may only narrow. Intersecting rather than replacing is the
    whole safety property: a caller cannot reach a namespace the deployment did
    not enable, whatever it asks for.

    A ``requested`` value that resolves to nothing within the ceiling is treated
    as no override at all — returning an empty tool list would look like a
    broken server rather than a rejected filter.
    """
    allowed = resolve_namespaces(parse_toolsets(startup))

    override = parse_toolsets(requested)
    if override:
        try:
            narrowed = resolve_namespaces(override) & allowed
        except UnknownToolsetError:
            # A bad header must not take down the request; fall back to the
            # deployment's own setting.
            narrowed = allowed
        if narrowed:
            allowed = narrowed

    return [name for name in names if namespace_of(name) in allowed]
