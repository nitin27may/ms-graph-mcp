"""Toolset profiles.

The property that matters most is the **ceiling**: an `X-Toolsets` header comes
from the caller, so it must only ever be able to narrow what the deployment
enabled. If it could widen, the header would be a way to reach namespaces an
operator deliberately turned off.

The second property is that this filters *visibility*, not *authority*. A tool
hidden from `tools/list` must still be refused at dispatch — a caller can name
any tool it likes, so the tier gates have to stand on their own.
"""

from __future__ import annotations

import json

import pytest

from ms_graph_mcp.allowlists import READ_TOOL_NAMES, WRITE_TOOL_NAMES
from ms_graph_mcp.config import GraphMcpConfig, set_config
from ms_graph_mcp.context import current_request_context
from ms_graph_mcp.toolsets import (
    ALL_PROFILE,
    PROFILES,
    UnknownToolsetError,
    filter_tool_names,
    namespace_of,
    parse_toolsets,
    resolve_namespaces,
)


class TestProfileResolution:
    def test_every_profile_resolves(self):
        for name in PROFILES:
            assert resolve_namespaces([name])

    def test_all_covers_every_namespace_any_profile_reaches(self):
        every = resolve_namespaces([ALL_PROFILE])
        for name in PROFILES:
            assert resolve_namespaces([name]) <= every

    def test_an_unknown_profile_raises_rather_than_being_ignored(self):
        """A typo that silently serves the wrong surface gives no signal."""
        with pytest.raises(UnknownToolsetError) as exc:
            resolve_namespaces(["maail"])
        assert "maail" in str(exc.value)
        assert "core" in str(exc.value), "the error should list the valid names"

    def test_no_profile_falls_back_to_core(self):
        assert resolve_namespaces([]) == resolve_namespaces(["core"])

    def test_profiles_combine(self):
        combined = resolve_namespaces(["mail", "calendar"])
        assert combined == resolve_namespaces(["mail"]) | resolve_namespaces(["calendar"])

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("mail,calendar", ["mail", "calendar"]),
            (" Mail , CALENDAR ", ["mail", "calendar"]),
            ("mail,,calendar", ["mail", "calendar"]),
            ("", []),
            (None, []),
        ],
    )
    def test_parsing_is_forgiving_about_whitespace_and_case(self, raw, expected):
        assert parse_toolsets(raw) == expected


class TestTheCeiling:
    """A caller-supplied override may narrow. It may never widen."""

    def test_a_request_cannot_reach_beyond_the_startup_setting(self):
        visible = filter_tool_names(READ_TOOL_NAMES, startup="mail", requested="all")
        namespaces = {namespace_of(n) for n in visible}
        assert namespaces <= {"mail"}, "a header asked for everything and got it"

    def test_a_request_cannot_add_a_namespace_the_deployment_omitted(self):
        visible = filter_tool_names(READ_TOOL_NAMES, startup="mail", requested="files")
        assert not [n for n in visible if namespace_of(n) == "files"]

    def test_a_request_can_narrow_within_the_ceiling(self):
        visible = filter_tool_names(READ_TOOL_NAMES, startup="core", requested="mail")
        assert {namespace_of(n) for n in visible} == {"mail"}
        assert len(visible) < len(filter_tool_names(READ_TOOL_NAMES, startup="core"))

    def test_an_override_disjoint_from_the_ceiling_falls_back_rather_than_emptying(self):
        """An empty tool list looks like a broken server, not a rejected filter."""
        visible = filter_tool_names(READ_TOOL_NAMES, startup="mail", requested="notes")
        assert visible, "should have fallen back to the startup profile"
        assert {namespace_of(n) for n in visible} == {"mail"}

    def test_a_malformed_header_does_not_break_the_request(self):
        visible = filter_tool_names(READ_TOOL_NAMES, startup="core", requested="not-a-profile")
        assert visible == filter_tool_names(READ_TOOL_NAMES, startup="core")


class TestFiltering:
    def test_core_is_a_genuine_subset(self):
        core = filter_tool_names(READ_TOOL_NAMES, "core")
        every = filter_tool_names(READ_TOOL_NAMES, "all")
        assert set(core) < set(every)
        assert len(every) == len(READ_TOOL_NAMES)

    def test_all_hides_nothing(self):
        assert set(filter_tool_names(READ_TOOL_NAMES, "all")) == set(READ_TOOL_NAMES)

    def test_filtering_never_invents_a_name(self):
        assert set(filter_tool_names(READ_TOOL_NAMES, "core")) <= set(READ_TOOL_NAMES)

    def test_a_profile_cannot_surface_a_write_tool_through_the_read_tier(self):
        """The tier split is upstream of profiles and must stay that way."""
        for profile in [*PROFILES, ALL_PROFILE]:
            visible = set(filter_tool_names(READ_TOOL_NAMES, profile))
            assert not visible & set(WRITE_TOOL_NAMES)

    def test_every_tool_belongs_to_some_namespace_all_reaches(self):
        """A tool in no profile would be invisible even under `all`."""
        every = resolve_namespaces([ALL_PROFILE])
        orphans = [n for n in (*READ_TOOL_NAMES, *WRITE_TOOL_NAMES) if namespace_of(n) not in every]
        assert not orphans, f"tools unreachable by any profile: {orphans}"


class TestVisibilityIsNotAuthority:
    """The point of the whole design: hiding is not gating."""

    async def test_a_tool_hidden_by_a_profile_is_not_advertised(self, list_tools):
        set_config(GraphMcpConfig(_env_file=None, toolsets="mail"))
        cv = current_request_context.set({"access_token": "tok"})
        try:
            names = {t.name for t in await list_tools()}
        finally:
            current_request_context.reset(cv)
        assert names, "mail profile should still advertise something"
        assert not [n for n in names if namespace_of(n) == "files"]

    async def test_a_write_tool_hidden_by_a_profile_is_still_refused_at_dispatch(self, call_tool):
        """A caller can name any tool. The tier gate, not the profile, stops it."""
        set_config(GraphMcpConfig(_env_file=None, toolsets="mail"))
        cv = current_request_context.set({"access_token": "tok", "write_scope": False})
        try:
            result = await call_tool("files_upload", {})
        finally:
            current_request_context.reset(cv)
        assert result.is_error is True
        assert json.loads(result.content[0].text)["error"] == "write_scope_required"

    async def test_a_read_tool_hidden_by_a_profile_still_dispatches(self, call_tool, monkeypatch):
        """Profiles are a context measure, not an access control.

        Narrowing what a client sees must not break a caller that already knows
        the name — otherwise the header becomes a footgun rather than a saving.
        """
        set_config(GraphMcpConfig(_env_file=None, toolsets="mail"))

        class _Reg:
            def canonical_name(self, name):
                return name

            async def call(self, name, arguments_json, context):
                return {"ok": True}

        monkeypatch.setattr("ms_graph_mcp.server.get_registry", lambda: _Reg())
        cv = current_request_context.set({"access_token": "tok"})
        try:
            result = await call_tool("files_search", {})
        finally:
            current_request_context.reset(cv)
        assert result.is_error is False

    async def test_the_header_narrows_the_advertised_surface(self, list_tools):
        set_config(GraphMcpConfig(_env_file=None, toolsets="core"))
        cv = current_request_context.set({"access_token": "tok", "toolsets": "mail"})
        try:
            names = {t.name for t in await list_tools()}
        finally:
            current_request_context.reset(cv)
        assert {namespace_of(n) for n in names} == {"mail"}

    async def test_the_header_cannot_widen_the_advertised_surface(self, list_tools):
        set_config(GraphMcpConfig(_env_file=None, toolsets="mail"))
        cv = current_request_context.set({"access_token": "tok", "toolsets": "all"})
        try:
            names = {t.name for t in await list_tools()}
        finally:
            current_request_context.reset(cv)
        assert {namespace_of(n) for n in names} == {"mail"}


class TestTokenCost:
    def test_the_default_profile_is_materially_cheaper_than_everything(self):
        """The reason the feature exists — measured, not assumed."""
        import ms_graph_mcp  # noqa: F401
        from ms_graph_mcp.tooling import get_registry

        registry = get_registry()

        def cost(names):
            return sum(
                len(
                    json.dumps(
                        {
                            "name": n,
                            "description": registry.get(n).description,
                            "inputSchema": registry.get(n).parameters,
                        }
                    )
                )
                for n in names
            )

        core = cost(filter_tool_names(READ_TOOL_NAMES, "core"))
        every = cost(filter_tool_names(READ_TOOL_NAMES, "all"))
        assert core < every / 2, (
            f"core is {core // 4} tokens vs {every // 4} for all — "
            "not enough of a saving to justify the config surface"
        )
