"""Contract tests for the graph-mcp read-only tool allowlist."""

from __future__ import annotations

import inspect

import pytest

from ms_graph_mcp.allowlists import (
    READ_TOOL_NAME_SET,
    READ_TOOL_NAMES,
    WRITE_TOOL_NAME_SET,
    WRITE_TOOL_NAMES,
    assert_no_write_in_reads,
    resolve_read_tools,
    resolve_write_tools,
)
from ms_graph_mcp.tooling import get_registry


def test_allowlist_has_no_duplicates():
    assert len(READ_TOOL_NAMES) == len(READ_TOOL_NAME_SET)


def test_allowlist_count_is_stable():
    # A regression guard so adding or dropping a tool is a deliberate edit
    # rather than an accident. Bump these when you mean to.
    assert len(READ_TOOL_NAMES) == 43
    assert len(WRITE_TOOL_NAMES) == 8


def test_resolve_read_tools_returns_registered_specs():
    specs = resolve_read_tools()
    assert len(specs) == len(READ_TOOL_NAMES)
    assert {spec.name for spec in specs} == READ_TOOL_NAME_SET


def test_every_read_tool_is_a_registered_async_tool():
    for spec in resolve_read_tools():
        assert inspect.iscoroutinefunction(spec.fn), f"{spec.name} must be async"
        assert getattr(spec.fn, "_is_tool", False), f"{spec.name} must be @tool-decorated"
        assert spec.description, f"{spec.name} must have a description"
        # OpenAI / MCP-compatible schema: an object with a properties map.
        assert spec.parameters.get("type") == "object", spec.name
        assert "properties" in spec.parameters, spec.name


def test_write_tools_are_excluded_from_read_allowlist():
    # The exclusion must be deliberate: each write tool is registered (so a
    # missing import is not what hides it) yet absent from the read allowlist.
    registry = get_registry()
    for name in WRITE_TOOL_NAMES:
        assert registry.get(name) is not None, f"{name} should be a registered tool"
        assert name not in READ_TOOL_NAME_SET, f"{name} is a write tool — must not be in reads"


def test_write_tools_resolvable():
    specs = resolve_write_tools()
    assert len(specs) == len(WRITE_TOOL_NAMES)
    assert {spec.name for spec in specs} == WRITE_TOOL_NAME_SET


def test_assert_no_write_in_reads_passes_for_current_allowlist():
    assert_no_write_in_reads()  # must not raise


def test_assert_no_write_in_reads_detects_a_leak(monkeypatch):
    import ms_graph_mcp.allowlists as tools_mod

    monkeypatch.setattr(
        tools_mod,
        "READ_TOOL_NAME_SET",
        tools_mod.READ_TOOL_NAME_SET | {"mail_send"},
    )
    with pytest.raises(RuntimeError, match="write tools"):
        tools_mod.assert_no_write_in_reads()
